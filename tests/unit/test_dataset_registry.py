from pathlib import Path

import yaml

REQUIRED = {"dataset_id", "class", "source", "license", "collection_date", "schema", "pii_status", "allowed_use",
            "version"}
CLASSES = {"research", "synthetic", "sandbox", "customer_provided", "production"}
USES = {"research", "internal_eval", "commercial_training"}


def test_registry_records_are_complete_and_consistent() -> None:
    data = yaml.safe_load((Path(__file__).resolve().parents[2] / "data" / "registry.yaml").read_text("utf-8"))
    ids = [d["dataset_id"] for d in data["datasets"]]
    assert len(ids) == len(set(ids))
    for d in data["datasets"]:
        assert REQUIRED <= set(d), d["dataset_id"]
        assert d["class"] in CLASSES
        assert set(d["allowed_use"]) <= USES
        # non-commercial / research-licensed data can never be used for commercial training
        if "NC" in d["license"] or d["class"] == "research":
            assert "commercial_training" not in d["allowed_use"], d["dataset_id"]
