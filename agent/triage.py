# -*- coding: utf-8 -*-
"""
Project 3d — triage agent with human-in-the-loop.

  python agent/triage.py "CRITICAL: HAProxy backend DOWN on host-01 (haproxy): backends_down=2"

Loop:  alert -> local LLM (qwen2.5:7b, tool calling) decides which MCP tool to call -> tool runs -> result back to LLM -> ...
       READ tools (get_alerts, search_runbooks, run_rca) run freely.
       WRITE tools (draft_ticket) STOP and ask the human:  approve? [y/N]   — nothing is written without a 'y'.
Every tool call is an OpenTelemetry span (tool.name, tool.args, tool.approved, tool.duration) under one agent.triage trace.
Safety rails: tool allowlist, max 8 tool calls, write tools need approval, dry-run flag (--dry-run auto-denies writes).
"""
import asyncio, json, os, sys, time, logging
logging.getLogger("httpx").setLevel(logging.WARNING)
from pathlib import Path

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project1-rca-assistant"))
import tracing                                                           # noqa: E402
tracer = tracing.init("triage-agent")

MODEL = "qwen2.5:7b"
MAX_TOOL_CALLS = 8
WRITE_TOOLS = {"draft_ticket"}                                           # everything else is read-only
ALLOWED_TOOLS = {"get_alerts", "search_runbooks", "run_rca", "draft_ticket"}
DRY_RUN = "--dry-run" in sys.argv

SYSTEM = """You are an SRE triage agent for an AIOps platform. You are given ONE alert.
Work step by step using the tools:
 1. get_alerts for the affected host to see related alerts.
 2. run_rca with a precise question about the alert to get a root cause with citations.
 3. If the RCA confidence is medium or high, call draft_ticket with a short title and a body that contains the root cause,
    the evidence and the next steps. If confidence is low, do NOT create a ticket — explain why instead.
Never invent hosts, commands or causes. Use only what the tools return.
Tools are invoked ONLY through the function-calling interface. Never write a tool call as text in your answer.
Never say a ticket was created unless the draft_ticket tool returned a ticket_id; if it was denied, say it was denied.
When finished, reply with a 3-line summary."""

NUDGE = ("You wrote a tool call as text instead of calling the tool. Nothing was executed. "
         "Call draft_ticket through the tools interface now, or if no ticket is needed, give the final summary without claiming one was created.")
MAX_NUDGES = 1


def mcp_tools_to_ollama(tools):
    schema = lambda t: getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}   # mcp 2.x / 1.x
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": schema(t)}} for t in tools if t.name in ALLOWED_TOOLS]


def approve(name, args):
    if DRY_RUN:
        print(f"   [dry-run] would call WRITE tool {name} — auto-denied"); return False
    print(f"\n   >>> WRITE ACTION requested: {name}({json.dumps(args, indent=None)[:400]})")
    ans = input("   approve? [y/N] ").strip().lower()
    return ans == "y"


async def main(alert_text):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server" / "tools.py")],
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            ollama_tools = mcp_tools_to_ollama(tools)
            print(f"[agent] tools from MCP server: {[t.name for t in tools]}")

            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"ALERT: {alert_text}"}]
            calls = nudges = 0
            with tracer.start_as_current_span("agent.triage") as root:
                root.set_attribute("agent.alert", alert_text); root.set_attribute("agent.model", MODEL); root.set_attribute("agent.dry_run", DRY_RUN)
                while True:
                    t = time.time()
                    with tracer.start_as_current_span("gen_ai.chat") as sp:
                        sp.set_attribute("gen_ai.system", "ollama"); sp.set_attribute("gen_ai.request.model", MODEL)
                        resp = ollama.chat(model=MODEL, messages=messages, tools=ollama_tools, options={"temperature": 0.1})
                        msg = resp["message"]
                        sp.set_attribute("gen_ai.usage.input_tokens", resp.get("prompt_eval_count", 0))
                        sp.set_attribute("gen_ai.usage.output_tokens", resp.get("eval_count", 0))
                    print(f"[llm] {time.time()-t:.0f}s  tool_calls={len(msg.get('tool_calls') or [])}")
                    messages.append(msg)

                    if not msg.get("tool_calls"):
                        content = msg.get("content") or ""
                        narrated = [n for n in ALLOWED_TOOLS if f"{n}(" in content]          # small models drift into "draft_ticket({...})" as prose
                        if narrated and nudges < MAX_NUDGES:
                            nudges += 1
                            print(f"[agent] model narrated {narrated} as text (no real tool call) -> nudging it to use the tool")
                            root.set_attribute("agent.narrated_tool_call", True)
                            messages.append({"role": "user", "content": NUDGE})
                            continue
                        print("\n=== AGENT SUMMARY ===\n" + (content or "(no content)"))
                        root.set_attribute("agent.tool_calls", calls)
                        break

                    for call in msg["tool_calls"]:
                        name = call["function"]["name"]; args = call["function"].get("arguments") or {}
                        calls += 1
                        with tracer.start_as_current_span("tool.call") as sp:
                            sp.set_attribute("tool.name", name); sp.set_attribute("tool.args", json.dumps(args)[:500])
                            if name not in ALLOWED_TOOLS:
                                result = {"error": f"tool {name} not allowed"}; sp.set_attribute("tool.allowed", False)
                            elif calls > MAX_TOOL_CALLS:
                                result = {"error": "tool-call budget exhausted"}; sp.set_attribute("tool.budget_exceeded", True)
                            elif name in WRITE_TOOLS and not approve(name, args):
                                result = {"denied": True, "reason": "human did not approve the write action"}
                                sp.set_attribute("tool.approved", False)
                            else:
                                if name in WRITE_TOOLS: sp.set_attribute("tool.approved", True)
                                t = time.time()
                                r = await session.call_tool(name, args)
                                text = r.content[0].text if r.content else "{}"
                                try:    result = json.loads(text)
                                except json.JSONDecodeError: result = {"text": text[:2000]}
                                if getattr(r, "is_error", None) or getattr(r, "isError", False): result = {"error": text[:500]}   # mcp 2.x / 1.x
                                sp.set_attribute("tool.duration_s", round(time.time() - t, 1))
                            short = json.dumps(result)[:160]
                            print(f"[tool] {name}({json.dumps(args)[:80]}) -> {short}")
                        messages.append({"role": "tool", "content": json.dumps(result)[:6000], "name": name})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    alert = " ".join(a for a in sys.argv[1:] if not a.startswith("--")) or \
            "CRITICAL: HAProxy backend DOWN on host-01 (haproxy): backends_down=2count (threshold 1count)"
    asyncio.run(main(alert))
