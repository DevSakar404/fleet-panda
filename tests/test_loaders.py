from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.data.loaders import (
    DataFileError,
    load_call_transcripts,
    load_knowledge_base,
    load_tenant_aliases,
    load_tenants,
    load_tickets,
)


def test_clean_fixtures_load_successfully() -> None:
    """Verify that all default repo data fixtures load without duplicate errors."""
    load_tenants.cache_clear()
    load_tenant_aliases.cache_clear()
    load_tickets.cache_clear()
    load_call_transcripts.cache_clear()
    load_knowledge_base.cache_clear()

    tenants = load_tenants()
    assert len(tenants) > 0

    aliases = load_tenant_aliases()
    assert len(aliases) > 0

    tickets = load_tickets()
    assert len(tickets) > 0

    calls = load_call_transcripts()
    assert len(calls) > 0

    kb = load_knowledge_base()
    assert len(kb) > 0


def test_load_tenants_raises_on_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_data = [
        {
            "tenant_id": 1,
            "name": "Company A",
            "health_score": 90,
            "carr": 50000,
            "modules_active": ["dispatch"],
            "contract_end_date": "2027-01-01",
            "assigned_csm": "Alice",
            "fleet_size": 10,
            "onboarding_status": "live",
            "region": "West",
        },
        {
            "tenant_id": 1,
            "name": "Company A Dup",
            "health_score": 85,
            "carr": 40000,
            "modules_active": ["dispatch"],
            "contract_end_date": "2027-01-01",
            "assigned_csm": "Alice",
            "fleet_size": 12,
            "onboarding_status": "live",
            "region": "West",
        },
    ]
    file_path = tmp_path / "customers.json"
    file_path.write_text(json.dumps(bad_data))

    monkeypatch.setattr("src.config.CUSTOMERS_PATH", file_path)
    load_tenants.cache_clear()

    with pytest.raises(DataFileError, match="duplicate tenant_id: 1"):
        load_tenants()


def test_load_tenant_aliases_raises_on_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_data = [
        {"alias": "apex", "canonical_name": "Apex Fuel", "tenant_id": 1},
        {"alias": "apex", "canonical_name": "Apex Fuel", "tenant_id": 1},
    ]
    file_path = tmp_path / "tenant_aliases.json"
    file_path.write_text(json.dumps(bad_data))

    monkeypatch.setattr("src.config.TENANT_ALIASES_PATH", file_path)
    load_tenant_aliases.cache_clear()

    with pytest.raises(DataFileError, match="Duplicate alias 'apex'"):
        load_tenant_aliases()


def test_load_tickets_raises_on_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_data = [
        {
            "ticket_id": 100,
            "tenant_id": 1,
            "tenant_name": "Company A",
            "subject": "Issue 1",
            "description": "Desc 1",
            "product_area": "dispatch",
            "status": "open",
            "priority": "high",
            "submitter_name": "Bob",
            "submitter_email": "bob@example.com",
            "agent_name": "Agent",
        },
        {
            "ticket_id": 100,
            "tenant_id": 1,
            "tenant_name": "Company A",
            "subject": "Issue 1 duplicate",
            "description": "Desc 1 dup",
            "product_area": "dispatch",
            "status": "open",
            "priority": "high",
            "submitter_name": "Bob",
            "submitter_email": "bob@example.com",
            "agent_name": "Agent",
        },
    ]
    file_path = tmp_path / "tickets.json"
    file_path.write_text(json.dumps(bad_data))

    monkeypatch.setattr("src.config.TICKETS_PATH", file_path)
    load_tickets.cache_clear()

    with pytest.raises(DataFileError, match="Duplicate ticket_id 100"):
        load_tickets()


def test_load_call_transcripts_raises_on_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_data = [
        {
            "call_id": "CALL-001",
            "tenant_name": "Company A",
            "topic": "Sync",
            "summary": "Summary",
            "sentiment": "positive",
        },
        {
            "call_id": "CALL-001",
            "tenant_name": "Company A",
            "topic": "Sync Dup",
            "summary": "Summary Dup",
            "sentiment": "positive",
        },
    ]
    file_path = tmp_path / "call_transcripts.json"
    file_path.write_text(json.dumps(bad_data))

    monkeypatch.setattr("src.config.CALL_TRANSCRIPTS_PATH", file_path)
    load_call_transcripts.cache_clear()

    with pytest.raises(DataFileError, match="Duplicate call_id 'CALL-001'"):
        load_call_transcripts()


def test_load_knowledge_base_raises_on_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_data = [
        {
            "article_id": "KB-001",
            "title": "Article 1",
            "product_area": "dispatch",
            "root_cause": "Cause 1",
            "resolution": "Resolution 1",
        },
        {
            "article_id": "KB-001",
            "title": "Article 1 dup",
            "product_area": "dispatch",
            "root_cause": "Cause 1 dup",
            "resolution": "Resolution 1 dup",
        },
    ]
    file_path = tmp_path / "knowledge_base.json"
    file_path.write_text(json.dumps(bad_data))

    monkeypatch.setattr("src.config.KNOWLEDGE_BASE_PATH", file_path)
    load_knowledge_base.cache_clear()

    with pytest.raises(DataFileError, match="Duplicate article_id 'KB-001'"):
        load_knowledge_base()
