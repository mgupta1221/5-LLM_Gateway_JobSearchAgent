"""
validate_prompt.py — Evaluate SYSTEM_PROMPT against the 8-criteria rubric.

Usage:
    python validate_prompt.py

Requires the llm_gatewayV2 to be running on port 8100.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "llm_gatewayV2"))
from client import LLM

from prompts import SYSTEM_PROMPT

RUBRIC_PROMPT = f"""You are a Prompt Evaluation Assistant. Your job is to evaluate a system prompt against 8 specific criteria and return a JSON verdict.

CRITERIA (each must be true or false):

1. explicit_reasoning — Does the prompt instruct the model to think step-by-step before every action and explain its reasoning after each tool result?

2. structured_output — Does the prompt enforce a strict, machine-parseable output format (e.g. FUNCTION_CALL / FINAL_ANSWER with a defined JSON schema)?

3. tool_separation — Does the prompt clearly separate WHEN to call tools vs WHEN to reason, with an explicit multi-turn sequence?

4. conversation_loop — Does the prompt demonstrate a multi-turn conversation flow with at least 2 turns shown (e.g. Turn 1, Turn 2, Turn 3)?

5. instructional_framing — Does the prompt include a complete worked example showing input, reasoning, tool calls, tool results, and final output?

6. internal_self_checks — Does the prompt require the model to run explicit validation checks (with PASS/FAIL verdicts) on tool results before using them? Look for numbered checks with specific conditions and recovery actions.

7. reasoning_type_awareness — Does the prompt define and require specific reasoning type labels (e.g. [LOOKUP], [ARITHMETIC], [LOGIC], [VERIFY]) to be prefixed on every reasoning step?

8. fallbacks — Does the prompt define specific FAILURE → ACTION recovery pairs for every failure mode (empty results, errors, insufficient data, unrealistic values)?

SYSTEM PROMPT TO EVALUATE:
--- START ---
{SYSTEM_PROMPT}
--- END ---

Return ONLY a JSON object with the 8 boolean keys and an "overall_clarity" string. No other text.
"""

def main():
    llm = LLM()
    print("Sending prompt to LLM for evaluation...\n")

    reply = llm.chat(
        prompt=RUBRIC_PROMPT,
        temperature=0,
        max_tokens=500,
    )

    provider = reply.get("provider", "?")
    model = reply.get("model", "?")
    text = reply.get("text", "").strip()

    print(f"Provider: {provider} | Model: {model}\n")
    print("=" * 60)
    print("EVALUATION RESULT:")
    print("=" * 60)
    print(text)
    print("=" * 60)

    # Check for any false values
    import json
    try:
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            criteria = [k for k in result if k != "overall_clarity"]
            all_true = all(result.get(k) is True for k in criteria)
            false_keys = [k for k in criteria if result.get(k) is not True]

            print(f"\nAll criteria passed: {'YES' if all_true else 'NO'}")
            if false_keys:
                print(f"Failed criteria: {', '.join(false_keys)}")
    except Exception:
        print("\n(Could not parse JSON from response)")


if __name__ == "__main__":
    main()
