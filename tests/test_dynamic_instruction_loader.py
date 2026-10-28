"""Tests verifying that instructions folder is directly and dynamically accessed by the LLM pipeline."""
import sys
from pathlib import Path
import pytest

root = Path(__file__).resolve().parent.parent
backend_dir = root / "backend"
sys.path.insert(0, str(backend_dir))

from llm_client import (
    get_instruction_files,
    get_instruction_doc_names,
    get_instruction_doc_name,
    get_official_instructions,
    get_system_prompt,
    INSTRUCTIONS_DIR,
)
from caching.config import get_instruction_hash
import census_config


def test_dynamic_instruction_discovery_and_swapping(caplog):
    """Verify that adding, modifying, or deleting instruction files is immediately detected and logged."""
    import logging
    caplog.set_level(logging.INFO, logger="census.llm")

    # 1. Baseline state
    initial_names = get_instruction_doc_names()
    initial_instructions = get_official_instructions()
    initial_hash = get_instruction_hash()
    assert len(initial_names) >= 1
    assert len(initial_instructions) > 0

    # 2. Add a new temporary instruction file
    temp_inst_path = INSTRUCTIONS_DIR / "AAA_Test_Custom_Experiment_Instruction.txt"
    try:
        temp_inst_path.write_text("CUSTOM EXPERIMENTAL RULE: All groups must route to Carrier Test X.", encoding="utf-8")

        # Immediately verify dynamic detection without restart or config change
        new_names = get_instruction_doc_names()
        assert temp_inst_path.name in new_names, f"Expected {temp_inst_path.name} in {new_names}"

        new_doc_name_str = get_instruction_doc_name()
        assert temp_inst_path.name in new_doc_name_str

        new_instructions = get_official_instructions()
        assert "CUSTOM EXPERIMENTAL RULE: All groups must route to Carrier Test X." in new_instructions

        # Assert that the new instruction name is logged in census.llm
        assert any(temp_inst_path.name in record.message for record in caplog.records), \
            f"Expected log entry mentioning {temp_inst_path.name}, got logs: {[r.message for r in caplog.records]}"

        new_system_prompt = get_system_prompt()
        assert temp_inst_path.name in new_system_prompt

        new_hash = get_instruction_hash()
        assert new_hash != initial_hash, "Instruction hash must change when instructions folder changes"

        # Verify dynamic access via census_config
        config_paths = census_config.get_instruction_paths()
        assert any(p.name == temp_inst_path.name for p in config_paths)
        assert any(p.name == temp_inst_path.name for p in census_config.INSTRUCTIONS_PATHS)

    finally:
        # 3. Clean up the temporary file
        if temp_inst_path.exists():
            temp_inst_path.unlink()

    # 4. Verify immediate reversion after deletion without restart or config change
    reverted_names = get_instruction_doc_names()
    assert temp_inst_path.name not in reverted_names
    reverted_instructions = get_official_instructions()
    assert "CUSTOM EXPERIMENTAL RULE" not in reverted_instructions
    reverted_hash = get_instruction_hash()
    assert reverted_hash == initial_hash
