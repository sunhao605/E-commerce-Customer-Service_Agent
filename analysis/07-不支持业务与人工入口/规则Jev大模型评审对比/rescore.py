"""只重算保存结果；原冻结标签、Prompt、规则及模型输出不改。"""
import json

from compare import DIMS, OUT, digest, normalize, outcome, rate, save


def score(rows, truth):
    result = {}
    for provider, items in rows.items():
        valid = [r for r in items if normalize(r.get("labels")) is not None]
        problems = [r for r in valid if outcome(truth[r["id"]]) == "reject"]
        clean = [r for r in valid if outcome(truth[r["id"]]) == "allow"]
        pending = [r for r in valid if outcome(truth[r["id"]]) == "review"]
        mismatches = [{"id": r["id"], "expected": truth[r["id"]], "actual": r["labels"]}
                      for r in valid if r["labels"] != truth[r["id"]]]
        result[provider] = {
            "rows": len(items), "valid": len(valid), "invalid_or_error": len(items) - len(valid),
            "overall_exact_agreement": rate(sum(outcome(r["labels"]) == outcome(truth[r["id"]])
                                                for r in valid), len(truth)),
            "all_dimensions_agree": rate(len(valid) - len(mismatches), len(truth)),
            "atomic_agreement": rate(sum(r["labels"][dim] == truth[r["id"]][dim]
                                         for r in valid for dim in DIMS), len(truth) * len(DIMS)),
            "missed_problem": rate(sum(outcome(r["labels"]) == "allow" for r in problems), len(problems)),
            "false_rejection": rate(sum(outcome(r["labels"]) == "reject" for r in clean), len(clean)),
            "pending_sent_to_review": rate(sum(outcome(r["labels"]) == "review" for r in pending), len(pending)),
            "dimensions": {dim: rate(sum(r["labels"][dim] == truth[r["id"]][dim] for r in valid),
                                     len(truth)) for dim in DIMS},
            "mismatches": mismatches,
        }
    return result


def main():
    review = json.loads((OUT / "labels-v2.json").read_text())
    frozen = json.loads((OUT / "frozen.json").read_text())
    verification = json.loads((OUT / "verification.json").read_text())
    assert digest(OUT / "frozen.json") == review["source_frozen_sha256"] == verification["frozen_sha256"]
    assert digest(OUT / "summary.json") == verification["summary_sha256"]
    rows = {}
    for provider in ("rules", "jev", "llm"):
        path = OUT / (provider + ".jsonl")
        assert digest(path) == review["prediction_sha256"][provider] == verification["result_sha256"][provider]
        rows[provider] = [json.loads(line) for line in path.read_text().splitlines()]
    original = {c["id"]: c["human_labels"] for c in frozen["cases"]}
    assert len(original) == 24 and set(review["labels"]) == set(original)
    assert all(normalize(value) is not None for value in review["labels"].values())
    assert all(len(items) == 24 and {r["id"] for r in items} == set(original) for items in rows.values())
    human_only = dict(original)
    for key in ("G14", "G22"):
        human_only[key] = review["labels"][key]
    revised = score(rows, review["labels"])
    direct = score(rows, human_only)
    old_score = score(rows, original)
    old_summary = json.loads((OUT / "summary.json").read_text())
    for provider in rows:
        assert old_score[provider]["overall_exact_agreement"] == old_summary["providers"][provider]["overall_exact_agreement"]
        assert old_score[provider]["all_dimensions_agree"] == old_summary["providers"][provider]["all_dimensions_agree"]
    assert outcome({"omission": "pass", "conflict": "uncertain", "unperformed": "fail"}) == "reject"
    assert [revised[p]["overall_exact_agreement"]["count"] for p in rows] == [16, 22, 24]
    assert [revised[p]["all_dimensions_agree"]["count"] for p in rows] == [12, 17, 21]
    assert [direct[p]["all_dimensions_agree"]["count"] for p in rows] == [15, 16, 23]
    summary = {"id": review["id"], "labels_sha256": digest(OUT / "labels-v2.json"),
               "annotation_source": review["annotation_source"], "limitations": review["limitations"],
               "remote_calls": 0, "production_changes": False, "original_artifacts_unchanged": True,
               "providers": revised, "human_corrections_only": direct, "original": old_score}
    save(OUT / "summary-v2.json", summary)
    print(json.dumps({p: {key: revised[p][key] for key in
                         ("overall_exact_agreement", "all_dimensions_agree", "atomic_agreement")}
                      for p in rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
