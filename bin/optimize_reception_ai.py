#!/usr/bin/env python3
# satpi
# Sends raw reception data to an AI backend and requests optimization advice.
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from load_config import load_config, ConfigError

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
MAX_OPTIMIZER_REPORT_CHARS = 12000


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze SATPI reception JSON via OpenAI or Ollama"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini (default: ../config/config.ini relative to this script)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Optional override for AI provider (openai or ollama)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional override for AI model",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional override for AI API base URL",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=200,
        help="Limit number of samples included in the prompt",
    )
    return parser.parse_args()


def get_config_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "config" / "config.ini"


def load_reception_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_latest_reception_json(base_dir: Path) -> Path:
    passes_dir = base_dir / "results" / "passes"

    if not passes_dir.exists():
        raise FileNotFoundError(f"Passes directory not found: {passes_dir}")

    files = sorted(
        passes_dir.glob("*-reception.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not files:
        raise FileNotFoundError(f"No reception JSON files found in: {passes_dir}")

    return files[0]


def reduce_payload(data: dict, max_samples: int) -> dict:
    reduced = dict(data)
    samples = data.get("samples", [])

    if len(samples) > max_samples:
        reduced["samples"] = samples[:max_samples]
        reduced["_truncated"] = True
        reduced["_original_sample_count"] = len(samples)
        reduced["_included_sample_count"] = len(reduced["samples"])
    else:
        reduced["_truncated"] = False
        reduced["_original_sample_count"] = len(samples)
        reduced["_included_sample_count"] = len(samples)

    return reduced


def load_text_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def find_optimizer_report(base_dir: Path, config: dict) -> Path | None:
    candidates = []

    optimize_output_dir = config["optimize_reception"].get("output_dir", "").strip()
    if optimize_output_dir:
        output_dir = Path(optimize_output_dir)
        if not output_dir.is_absolute():
            output_dir = base_dir / output_dir
        candidates.append(output_dir / "optimization-report.txt")

    candidates.append(base_dir / "results" / "optimization" / "optimization-report.txt")
    candidates.append(Path(config["paths"]["optimization_dir"]) / "optimization-report.txt")
    candidates.append(Path(config["paths"]["optimization_ai_report_file"]).with_name("optimization-report.txt"))

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved

    return None


def load_optimizer_report(base_dir: Path, config: dict) -> tuple[str | None, str | None]:
    report_path = find_optimizer_report(base_dir, config)
    if report_path is None:
        return None, None

    report_text = load_text_file(report_path).strip()
    if not report_text:
        return str(report_path), None

    if len(report_text) > MAX_OPTIMIZER_REPORT_CHARS:
        report_text = (
            report_text[:MAX_OPTIMIZER_REPORT_CHARS].rstrip()
            + "\n\n[truncated for prompt size]"
        )

    return str(report_path), report_text


def build_prompt(
    payload: dict,
    include_optimizer_report: bool,
    optimizer_report_text: str | None = None,
    optimizer_report_path: str | None = None,
) -> str:
    context_note = (
        "Treat the raw reception JSON as the primary evidence. "
        "If an optimizer report is provided, use it as secondary derived context, "
        "not as ground truth.\n"
        "If the raw telemetry and the optimizer report disagree, call out the conflict explicitly.\n"
        "If the data is incomplete or ambiguous, say so explicitly and propose what additional telemetry or logs should be included next time.\n"
    )

    optimizer_report_section = ""
    if include_optimizer_report and optimizer_report_text:
        optimizer_report_section = f"""

Classic optimizer report (derived heuristic analysis, supplemental context only):
Source: {optimizer_report_path}
{optimizer_report_text}
""".rstrip()
    elif include_optimizer_report:
        optimizer_report_section = """

Classic optimizer report:
Requested, but no optimizer report text was available. State that this supplemental context is missing if it affects confidence.
""".rstrip()

    return f"""
You are analyzing raw satellite reception telemetry from an autonomous SDR reception pipeline.

Task:
1. Analyze the raw reception data first.
2. Identify likely technical causes of poor reception quality.
3. Suggest concrete optimization actions.
4. Focus on antenna visibility, gain setting, frequency stability, horizon blockage, sync behavior, pass geometry, and data completeness.
5. Distinguish clearly between:
   - confirmed observations from the data
   - findings suggested by the classic optimizer report
   - plausible hypotheses
   - recommended next tests
6. Be specific, technical, and concise.
7. Do not invent measurements that are not present in the data.
8. If there are signs that the telemetry logging itself is incomplete or misleading, point that out clearly.
9. Prefer conclusions supported directly by telemetry. Use the optimizer report only to reinforce, challenge, or prioritize follow-up checks.
{context_note}
Please structure the answer with these sections:
- Summary
- Observations from the data
- Findings from the optimizer report
- Most likely causes
- Recommended actions
- What to measure next

Raw reception JSON:
{json.dumps(payload, indent=2)}
{optimizer_report_section}
""".strip()


def write_output_file(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")


def normalize_provider(value: str | None) -> str:
    return (value or DEFAULT_PROVIDER).strip().lower()


def call_openai(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    timeout_seconds: int,
) -> str:
    try:
        from openai import OpenAI, APIError, RateLimitError
    except ImportError as e:
        raise RuntimeError(
            "OpenAI client library is not installed. Install python3-openai or switch "
            "optimize_reception_ai.provider to ollama."
        ) from e

    client_kwargs = {
        "api_key": api_key,
        "timeout": timeout_seconds,
    }
    if base_url:
        client_kwargs["base_url"] = base_url.rstrip("/")

    client = OpenAI(**client_kwargs)

    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            temperature=temperature,
        )
    except RateLimitError as e:
        raise RuntimeError(f"OpenAI API quota/rate-limit problem: {e}") from e
    except APIError as e:
        raise RuntimeError(f"OpenAI API error: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error during OpenAI request: {e}") from e

    result_text = (response.output_text or "").strip()
    if not result_text:
        raise RuntimeError("OpenAI response did not contain any text output")

    return result_text


def call_ollama(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    timeout_seconds: int,
) -> str:
    request_body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
        },
    }
    endpoint = f"{base_url.rstrip('/')}/api/generate"
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Ollama HTTP error {e.code} from {endpoint}: {error_body}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama connection error for {endpoint}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error during Ollama request: {e}") from e

    try:
        response_json = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama returned invalid JSON: {e}") from e

    if response_json.get("error"):
        raise RuntimeError(f"Ollama error: {response_json['error']}")

    result_text = str(response_json.get("response", "")).strip()
    if not result_text:
        raise RuntimeError("Ollama response did not contain any generated text")

    return result_text


def request_analysis(
    provider: str,
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    timeout_seconds: int,
) -> str:
    if provider == "openai":
        return call_openai(
            prompt=prompt,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

    if provider == "ollama":
        return call_ollama(
            prompt=prompt,
            model=model,
            api_key=api_key,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

    raise RuntimeError(f"Unsupported AI provider: {provider}")


def main():
    args = parse_args()

    config_path = get_config_path(args.config)

    try:
        config = load_config(str(config_path))
    except ConfigError as e:
        print(f"[ERROR] CONFIG ERROR: {e}")
        return 1

    ai_cfg = config["optimize_reception_ai"]

    if not ai_cfg["enabled"]:
        print("[INFO] optimize_reception_ai is disabled in config.ini")
        return 0

    provider = normalize_provider(args.provider or ai_cfg["provider"])
    model = args.model or ai_cfg["model"] or DEFAULT_MODEL
    base_url = (args.base_url or ai_cfg["base_url"]).strip()
    api_key = ai_cfg["api_key"]
    temperature = ai_cfg["temperature"]
    timeout_seconds = ai_cfg["request_timeout_seconds"]
    include_optimizer_report = ai_cfg["include_optimizer_report"]
    output_file = Path(config["paths"]["optimization_ai_report_file"])
    base_dir = Path(config["paths"]["base_dir"])

    if provider == "openai" and not api_key:
        print("[ERROR] optimize_reception_ai.api_key is empty for provider=openai")
        return 1

    try:
        reception_path = find_latest_reception_json(base_dir)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1

    print(f"[INFO] Using config: {config_path}")
    print(f"[INFO] Using reception JSON: {reception_path}")
    print(f"[INFO] Provider: {provider}")
    print(f"[INFO] Model: {model}")
    if base_url:
        print(f"[INFO] Base URL: {base_url}")

    data = load_reception_json(reception_path)
    payload = reduce_payload(data, args.max_samples)
    optimizer_report_path = None
    optimizer_report_text = None
    if include_optimizer_report:
        optimizer_report_path, optimizer_report_text = load_optimizer_report(
            base_dir,
            config,
        )
        if optimizer_report_path and optimizer_report_text:
            print(f"[INFO] Using optimizer report: {optimizer_report_path}")
        elif optimizer_report_path:
            print(f"[WARN] Optimizer report is empty: {optimizer_report_path}")
        else:
            print("[WARN] include_optimizer_report=true but optimization-report.txt was not found")

    prompt = build_prompt(
        payload,
        include_optimizer_report,
        optimizer_report_text=optimizer_report_text,
        optimizer_report_path=optimizer_report_path,
    )

    try:
        result_text = request_analysis(
            provider=provider,
            prompt=prompt,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return 1

    print(result_text)

    try:
        write_output_file(output_file, result_text)
        print(f"[INFO] Report written to: {output_file}")
    except Exception as e:
        print(f"[ERROR] Could not write output file: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
