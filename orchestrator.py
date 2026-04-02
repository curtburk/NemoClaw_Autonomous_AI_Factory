#!/usr/bin/env python3
"""
NemoClaw Autonomous Software Factory
=====================================
Orchestrates four isolated NemoClaw sandboxes to build a city infrastructure
health dashboard from a plain-English specification.

Pipeline:
  User Spec → [Architect] → Plan → [Coder] → Code → [Reviewer] → Reviewed Code → [Analyst] → Dashboard

Each agent runs in its own NemoClaw sandbox (Landlock + seccomp + netns).
The orchestrator streams live status to a browser UI via WebSocket.
A golden-path fallback ensures the demo never fails live.
"""

import subprocess
import sys
import os
import json
import time
import re
import asyncio
import threading
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

SANDBOXES = {
    "architect": "openshell-architect",
    "coder":     "openshell-coder",
    "reviewer":  "openshell-reviewer",
    "analyst":   "openshell-analyst",
}

BASE_DIR    = Path(__file__).parent
OUTPUT_DIR  = BASE_DIR / "output"
LOGS_DIR    = BASE_DIR / "logs"
PROMPTS_DIR = BASE_DIR / "prompts"
FALLBACK_DIR = BASE_DIR / "fallback"
MAX_RETRIES = 3
SSH_TIMEOUT = 900  # seconds — Analyst generates large HTML dashboards

# Global state for WebSocket streaming
RUN_ID = None
_ws_clients = set()
_log_buffer = []

# ── WebSocket broadcasting ────────────────────────────────────────────────────

def broadcast(event_type: str, data: dict):
    """Queue a JSON event for all connected WebSocket clients."""
    event = {"type": event_type, "timestamp": datetime.now().isoformat(), **data}
    _log_buffer.append(event)
    # If the asyncio loop is running, schedule the broadcast
    for ws_queue in list(_ws_clients):
        try:
            ws_queue.put_nowait(event)
        except Exception:
            pass

# ── Prompt loader ─────────────────────────────────────────────────────────────

def load_prompt(filename: str, **kwargs) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    template = prompt_path.read_text()
    try:
        return template.format_map(kwargs)
    except KeyError as e:
        raise ValueError(f"Missing placeholder {e} in prompt file {filename}")

# ── Logging ───────────────────────────────────────────────────────────────────

def log(stage: str, message: str, level: str = "info"):
    """Print timestamped status, append to log file, and broadcast to UI."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{stage.upper()}] {message}"
    print(line, flush=True)

    # Write to log file
    log_file = LOGS_DIR / f"run_{RUN_ID}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")

    # Broadcast to WebSocket clients
    broadcast("log", {"stage": stage, "message": message, "level": level})

def banner(title: str):
    width = 60
    sep = "─" * width
    print(f"\n{sep}\n  {title}\n{sep}", flush=True)
    broadcast("stage", {"title": title})

# ── Core: SSH into sandbox and run openclaw ───────────────────────────────────

def run_agent(sandbox_name: str, prompt: str, session_id: str) -> str:
    """
    SSH into a NemoClaw sandbox and send a prompt to the OpenClaw agent.

    openclaw agent requires -m <text>, but passing long prompts as shell
    arguments breaks on special characters. Instead we:
      1. Write the prompt to /tmp/prompt.txt inside the sandbox
      2. Use $(cat /tmp/prompt.txt) as the -m value
    """
    ssh_host = SANDBOXES[sandbox_name]
    ssh_opts = [
        "ssh",
        "-o", "ConnectTimeout=30",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        ssh_host,
    ]

    # Save prompt locally for debugging
    prompt_file = LOGS_DIR / f"{RUN_ID}_{sandbox_name}_prompt.txt"
    prompt_file.write_text(prompt)

    log(sandbox_name, f"Sending prompt ({len(prompt)} chars)...")
    broadcast("agent_start", {"agent": sandbox_name, "prompt_length": len(prompt)})

    # Step 1: Write prompt to file inside the sandbox via stdin
    # Using cat > to handle any content safely
    upload_cmd = ssh_opts + ["cat > /tmp/prompt.txt"]
    try:
        upload_result = subprocess.run(
            upload_cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30
        )
        if upload_result.returncode != 0:
            raise RuntimeError(
                f"[{sandbox_name}] Failed to upload prompt: {upload_result.stderr}"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"[{sandbox_name}] Prompt upload timed out")

    # Step 2: Run the agent with $(cat /tmp/prompt.txt) as the -m value
    # Use a unique session ID per call to prevent context window accumulation
    import uuid
    call_session = f"{session_id}-{sandbox_name}-{uuid.uuid4().hex[:8]}"
    agent_cmd = ssh_opts + [
        f'openclaw agent --agent main --local --thinking off --session-id {call_session} '
        f'-m "$(cat /tmp/prompt.txt)"'
    ]

    # Heartbeat thread
    stop_heartbeat = threading.Event()
    def heartbeat():
        elapsed = 0
        while not stop_heartbeat.wait(30):
            elapsed += 30
            log(sandbox_name, f"Still waiting... ({elapsed}s elapsed)")
    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    try:
        result = subprocess.run(
            agent_cmd,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        stop_heartbeat.set()
        raise RuntimeError(f"[{sandbox_name}] Timed out after {SSH_TIMEOUT}s")
    finally:
        stop_heartbeat.set()

    # Filter out Node.js warnings from stderr
    stderr_clean = "\n".join(
        line for line in result.stderr.splitlines()
        if "UNDICI" not in line and "experimental" not in line.lower()
    )

    if result.returncode != 0:
        log(sandbox_name, f"SSH failed (exit {result.returncode}): {stderr_clean[:300]}", "error")
        raise RuntimeError(
            f"[{sandbox_name}] SSH command failed (exit {result.returncode}):\n{stderr_clean}"
        )

    response = result.stdout.strip()
    if not response:
        raise RuntimeError(f"[{sandbox_name}] Empty response from agent")

    log(sandbox_name, f"Response received ({len(response)} chars)")
    broadcast("agent_complete", {"agent": sandbox_name, "response_length": len(response)})

    # Save raw response for debugging
    (LOGS_DIR / f"{RUN_ID}_{sandbox_name}_response.txt").write_text(response)

    return response

# ── Code validation ───────────────────────────────────────────────────────────

def validate_code(code: str, working_dir: Path) -> tuple:
    """
    Extract Python code from agent response, write to generated_app.py,
    and validate via syntax check + import dry-run.

    Returns (success: bool, output: str)
    """
    # Extract code from markdown fences if present
    python_blocks = re.findall(r"```python\s*\n(.*?)```", code, re.DOTALL)
    if python_blocks:
        executable = "\n\n".join(python_blocks)
    else:
        executable = code.strip()
        if executable.startswith("```"):
            executable = re.sub(r'^```\w*\s*\n?', '', executable)
            executable = re.sub(r'\n?```$', '', executable)
            executable = executable.strip()

    # Write to output dir
    code_file = working_dir / "generated_app.py"
    code_file.write_text(executable)
    log("validator", f"Wrote {len(executable)} chars to {code_file.name}")

    # Step 1: syntax check
    log("validator", "Running syntax check...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(code_file)],
            capture_output=True, text=True, timeout=15, cwd=str(working_dir)
        )
        if result.returncode != 0:
            return False, f"Syntax error:\n{result.stderr or 'No details'}"
    except subprocess.TimeoutExpired:
        return False, "Syntax check timed out"

    log("validator", "Syntax OK — running import check...")

    # Step 2: import check — suppress uvicorn.run so it doesn't block
    safe_code = re.sub(
        r'if\s+__name__\s*==\s*["\']__main__["\']\s*:.*$',
        'pass  # entry point suppressed for validation',
        executable, flags=re.DOTALL
    )
    # Also suppress any bare uvicorn.run calls
    safe_code = re.sub(
        r'uvicorn\.run\s*\(.*?\)',
        'pass  # uvicorn.run suppressed',
        safe_code, flags=re.DOTALL
    )
    safe_file = working_dir / "_validate_app.py"
    safe_file.write_text(safe_code)

    try:
        result = subprocess.run(
            [sys.executable, str(safe_file)],
            capture_output=True, text=True, timeout=15, cwd=str(working_dir)
        )
        safe_file.unlink(missing_ok=True)
        if result.returncode != 0:
            error = result.stderr or result.stdout or "Import failed"
            return False, f"Import/runtime error:\n{error}"
    except subprocess.TimeoutExpired:
        safe_file.unlink(missing_ok=True)
        return False, "Import check timed out"

    return True, "Syntax and import checks passed"

# ── Agent stages ──────────────────────────────────────────────────────────────

def run_architect(spec: str, session_id: str) -> str:
    banner("STAGE 1 / 4 — ARCHITECT")
    log("architect", "Decomposing specification into implementation plan...")
    prompt = load_prompt("architect.txt", spec=spec)
    plan = run_agent("architect", prompt, session_id)
    log("architect", "Implementation plan complete.")
    return plan


def run_coder(plan: str, session_id: str, feedback: str = None) -> str:
    is_retry = feedback is not None
    banner("STAGE 2 / 4 — CODER" + (" (retry)" if is_retry else ""))

    if is_retry:
        log("coder", "Received error feedback — revising implementation...")
        prompt = load_prompt("coder_retry.txt", plan=plan, error=feedback)
    else:
        log("coder", "Implementing plan...")
        prompt = load_prompt("coder_initial.txt", plan=plan)

    code = run_agent("coder", prompt, session_id)
    log("coder", "Code generation complete.")
    return code


def run_reviewer(plan: str, code: str, session_id: str) -> tuple:
    banner("STAGE 3 / 4 — REVIEWER")
    log("reviewer", "Reviewing code...")
    prompt = load_prompt("reviewer.txt", code=code)
    review = run_agent("reviewer", prompt, session_id)
    log("reviewer", "Review complete.")

    approved = "VERDICT: APPROVE" in review
    log("reviewer", f"Verdict: {'APPROVED ✓' if approved else 'REVISE ✗'}",
        "info" if approved else "warning")

    if not approved:
        issues_match = re.search(r"ISSUES:\s*(.*?)(?:REVISED CODE:|$)", review, re.DOTALL)
        if issues_match:
            for line in issues_match.group(1).strip().splitlines():
                if line.strip():
                    log("reviewer", f"  Issue: {line.strip()}", "warning")

    return review, approved


def run_analyst(plan: str, session_id: str) -> str:
    banner("STAGE 4 / 4 — ANALYST")
    log("analyst", "Generating data function...")

    # Ask the model to produce ONLY the generateData() JavaScript function
    prompt = (PROMPTS_DIR / "analyst.txt").read_text()
    gen_func = run_agent("analyst", prompt, session_id)

    # Clean up — strip markdown fences if present
    gen_func = gen_func.strip()
    if gen_func.startswith("```"):
        gen_func = re.sub(r'^```\w*\s*\n?', '', gen_func)
    if gen_func.endswith("```"):
        gen_func = gen_func[:-3].rstrip()

    # Strip OpenClaw tool-call artifacts that the model sometimes emits
    # These look like </parameter>\n<parameter=...>\n</function>\n</tool_call>
    gen_func = re.sub(r'</parameter>.*', '', gen_func, flags=re.DOTALL)
    gen_func = re.sub(r'<parameter[^>]*>.*', '', gen_func, flags=re.DOTALL)
    gen_func = re.sub(r'</function>.*', '', gen_func, flags=re.DOTALL)
    gen_func = re.sub(r'</tool_call>.*', '', gen_func, flags=re.DOTALL)
    gen_func = re.sub(r'<function[^>]*>.*', '', gen_func, flags=re.DOTALL)
    gen_func = gen_func.strip()

    # Strip any leading prose before the function
    func_idx = gen_func.find("function generateData")
    if func_idx == -1:
        func_idx = gen_func.find("function r(")  # might include helper functions
    if func_idx > 0:
        log("analyst", f"Trimming {func_idx} chars of leading prose")
        gen_func = gen_func[func_idx:]

    # Find the last closing brace of the function and truncate anything after
    # Count braces to find where generateData() ends
    brace_count = 0
    end_idx = len(gen_func)
    in_func = False
    for i, ch in enumerate(gen_func):
        if ch == '{':
            brace_count += 1
            in_func = True
        elif ch == '}':
            brace_count -= 1
            if in_func and brace_count == 0:
                end_idx = i + 1
                # Don't break — there might be a second function after
    gen_func = gen_func[:end_idx].strip()

    log("analyst", f"Generated data function ({len(gen_func)} chars)")

    # Load the dashboard template and inject the function
    template = (PROMPTS_DIR / "dashboard_template.html").read_text()

    # Add helper functions if the model didn't include them
    helpers = ""
    if "function r(" not in gen_func:
        helpers = "function r(a,b){return Math.random()*(b-a)+a}\nfunction ri(a,b){return Math.floor(Math.random()*(b-a+1))+a}\n\n"

    dashboard_html = template.replace("%%GENERATE_DATA%%", helpers + gen_func)

    # Verify injection worked
    if "generateData" in dashboard_html:
        log("analyst", "✓ Data function injected into dashboard template")
    else:
        log("analyst", "✗ Data function injection failed", "error")

    log("analyst", f"Dashboard complete ({len(dashboard_html)} chars)")
    return dashboard_html

# ── Golden-path fallback ─────────────────────────────────────────────────────

def activate_fallback():
    """Copy pre-baked golden-path files to output directory."""
    log("fallback", "Activating golden-path fallback...", "warning")
    broadcast("fallback", {"reason": "Pipeline failed, using pre-baked output"})

    fallback_dashboard = FALLBACK_DIR / "dashboard.html"
    fallback_app = FALLBACK_DIR / "generated_app.py"

    if fallback_dashboard.exists():
        import shutil
        shutil.copy2(fallback_dashboard, OUTPUT_DIR / "dashboard.html")
        log("fallback", "Copied fallback dashboard.html")
    else:
        log("fallback", "No fallback dashboard.html found!", "error")

    if fallback_app.exists():
        import shutil
        shutil.copy2(fallback_app, OUTPUT_DIR / "generated_app.py")
        log("fallback", "Copied fallback generated_app.py")

# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(spec: str):
    global RUN_ID
    RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)

    session_id = f"factory-{RUN_ID}"

    header = f"""
{'═' * 60}
  NEMOCLAW AUTONOMOUS SOFTWARE FACTORY
  City Infrastructure Health Dashboard
{'═' * 60}
  Run ID:  {RUN_ID}
  Session: {session_id}
  Spec:    {spec[:80]}{'...' if len(spec) > 80 else ''}
{'═' * 60}"""
    print(header, flush=True)
    broadcast("pipeline_start", {
        "run_id": RUN_ID,
        "session_id": session_id,
        "spec_preview": spec[:200]
    })

    start_time = time.time()
    use_fallback = False

    try:
        # ── Stage 1: Architect ────────────────────────────────────────
        plan = run_architect(spec, session_id)
        (LOGS_DIR / f"{RUN_ID}_plan.txt").write_text(plan)

        # ── Stage 2: Coder with self-healing loop ─────────────────────
        code = run_coder(plan, session_id)
        feedback = None
        code_valid = False

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                log("orchestrator", f"Retry {attempt}/{MAX_RETRIES}")
                code = run_coder(plan, session_id, feedback=feedback)

            log("validator", f"Validating code (attempt {attempt}/{MAX_RETRIES})...")
            code_valid, output = validate_code(code, OUTPUT_DIR)

            if code_valid:
                log("validator", "✓ Code validated successfully")
                break
            else:
                log("validator", f"✗ Validation failed:\n{output[:500]}", "error")
                feedback = output

        if not code_valid:
            log("orchestrator", f"Coder failed after {MAX_RETRIES} attempts", "error")
            use_fallback = True

        (LOGS_DIR / f"{RUN_ID}_code.py").write_text(code)

        if not use_fallback:
            # ── Stage 3: Reviewer ─────────────────────────────────────
            review, approved = run_reviewer(plan, code, session_id)
            (LOGS_DIR / f"{RUN_ID}_review.txt").write_text(review)

            if not approved:
                revised_blocks = re.findall(r"```python\n(.*?)```", review, re.DOTALL)
                if revised_blocks:
                    log("orchestrator", "Reviewer provided revised code — validating...")
                    revised_code = revised_blocks[0]

                    # Validate the reviewer's revised code before accepting it
                    rev_valid, rev_output = validate_code(revised_code, OUTPUT_DIR)
                    if rev_valid:
                        log("orchestrator", "✓ Reviewer's revised code passed validation")
                        code = revised_code
                        (LOGS_DIR / f"{RUN_ID}_code_revised.py").write_text(code)
                    else:
                        log("orchestrator", f"✗ Reviewer's code also broken — sending issues back to Coder", "warning")
                        # Extract the issues list and send to Coder for one more attempt
                        issues_match = re.search(r"ISSUES:\s*(.*?)(?:REVISED CODE:|$)", review, re.DOTALL)
                        issues_text = issues_match.group(1).strip() if issues_match else "Code is incomplete"
                        code = run_coder(plan, session_id, feedback=f"Reviewer found these issues:\n{issues_text}\n\nValidation error:\n{rev_output}")
                        final_valid, final_output = validate_code(code, OUTPUT_DIR)
                        if final_valid:
                            log("orchestrator", "✓ Coder fix after review passed validation")
                        else:
                            log("orchestrator", "✗ Coder still failing after review — using fallback", "error")
                            use_fallback = True
                else:
                    # Reviewer said REVISE but gave no code — send issues to Coder
                    log("orchestrator", "Reviewer flagged issues but no revised code — sending to Coder")
                    issues_match = re.search(r"ISSUES:\s*(.*?)$", review, re.DOTALL)
                    issues_text = issues_match.group(1).strip() if issues_match else "Code needs revision"
                    code = run_coder(plan, session_id, feedback=f"Reviewer found these issues:\n{issues_text}")
                    final_valid, final_output = validate_code(code, OUTPUT_DIR)
                    if final_valid:
                        log("orchestrator", "✓ Coder fix after review passed validation")
                    else:
                        log("orchestrator", "✗ Coder still failing after review — using fallback", "error")
                        use_fallback = True

            # ── Stage 4: Analyst builds standalone dashboard ──────────
            dashboard_html = run_analyst(plan, session_id)
            dashboard_path = OUTPUT_DIR / "dashboard.html"
            dashboard_path.write_text(dashboard_html)
            log("analyst", f"Dashboard saved to {dashboard_path}")

    except Exception as e:
        log("orchestrator", f"Pipeline error: {type(e).__name__}: {e}", "error")
        use_fallback = True

    if use_fallback:
        activate_fallback()

    elapsed = time.time() - start_time

    summary = f"""
{'═' * 60}
  PIPELINE COMPLETE
{'═' * 60}
  Time elapsed:  {elapsed:.1f}s
  Dashboard:     {OUTPUT_DIR / 'dashboard.html'}
  Backend code:  {OUTPUT_DIR / 'generated_app.py'}
  Logs:          {LOGS_DIR / f'run_{RUN_ID}.log'}
  Fallback used: {'YES' if use_fallback else 'No'}
{'═' * 60}

  Open dashboard:  file://{(OUTPUT_DIR / 'dashboard.html').resolve()}
"""
    print(summary, flush=True)
    broadcast("pipeline_complete", {
        "elapsed_seconds": round(elapsed, 1),
        "fallback_used": use_fallback,
        "dashboard_path": str(OUTPUT_DIR / "dashboard.html")
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SPEC = """
    Build a real-time city infrastructure health monitoring system for a mid-sized
    city with 6 districts. Monitor three systems: power grid (voltage, load, outages),
    water network (pressure, flow rate, quality index), and traffic (congestion index,
    incident count, average speed). Generate realistic sensor data with occasional
    anomalies — a power fluctuation in district 3, a water pressure drop in district 5,
    and a traffic gridlock event in district 1. Serve everything via a FastAPI backend.
    """
    try:
        run_pipeline(SPEC)
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Pipeline cancelled by user.")
        sys.exit(0)
    except Exception as e:
        ts = datetime.now().strftime("%H:%M:%S")
        crash_line = f"[{ts}] [CRASH] {type(e).__name__}: {e}"
        print(crash_line)
        try:
            LOGS_DIR.mkdir(exist_ok=True)
            with open(LOGS_DIR / f"run_{RUN_ID}.log", "a") as f:
                f.write(crash_line + "\n")
        except Exception:
            pass
        sys.exit(1)