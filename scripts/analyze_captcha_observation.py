"""Read the dated redacted trace; print evidence without changing booking state."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def analyze(payload: dict) -> dict:
    workers = payload.get("workers", [])
    chain = sorted(
        ({**event, "worker_index": worker["worker_index"],
          "account_fingerprint": worker["account_fingerprint"]}
         for worker in workers for event in worker.get("events", [])),
        key=lambda event: event.get("start_offset_ms", 0),
    )
    submits = [e for e in chain if e["path"].endswith("/reservation/order/submit")]
    token_uses = defaultdict(list)
    clients = defaultdict(set)
    for event in chain:
        if event.get("client_uid_fingerprint"):
            clients[event["client_uid_fingerprint"]].add(event["worker_index"])
    for event in submits:
        token = event.get("request", {}).get("root.captchaToken", {}).get("correlation")
        if token:
            token_uses[token].append(event["id"])
    invalid = [e for e in submits if "验证码非法校验" in e.get("response", {}).get("root.message", "")]
    return {
        "run_id": payload.get("run_id"), "worker_count": len(workers),
        "expected_worker_count": payload.get("expected_worker_count"),
        "worker_outcomes": dict(Counter(w.get("outcome", {}).get("reason", "missing") for w in workers)),
        "event_count": len(chain), "submit_count": len(submits),
        "invalid_submit_count": len(invalid),
        "rate_limit_submit_count": sum("验证码次数超出限制" in e.get("response", {}).get("root.message", "") for e in submits),
        "duplicate_token_submit_ids": [ids for ids in token_uses.values() if len(ids) > 1],
        "shared_client_uid_workers": [sorted(indices) for indices in clients.values() if len(indices) > 1],
        "capture_health": [{
            "worker": w["worker_index"], "outcome_recorded": bool(w.get("outcome")),
            "dropped": w.get("dropped_requests", 0), "widget_asset_match": w.get("widget_asset_match"),
            "capture_errors": w.get("capture_errors", []),
            "summary_drain_ms": w.get("summary_drain_ms"),
            "incomplete_requests": sum(bool(e.get("incomplete_at_worker_end")) for e in w.get("events", [])),
            "read_errors": sum(bool(e.get("headers_error") or e.get("response_error")) for e in w.get("events", [])),
        } for w in workers],
        "invalid_request_evidence": [{
            "id": e["id"], "worker": e["worker_index"], "attempt": e.get("attempt"),
            "start_offset_ms": e.get("start_offset_ms"),
            "matched_check": e.get("matched_check_request_id"),
            "latest_check_token_match": e.get("latest_check_booking_token_match"),
            "consistency": e.get("consistency"), "generation_check_error": e.get("generation_check_error"),
            "response": e.get("response"),
        } for e in invalid],
        "caution": "Shared identities or overlap alone do not establish causation. Compare complete successful/failed chains; server logs may still be needed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.trace.read_text(encoding="utf-8"))), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
