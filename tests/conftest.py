from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


def load_transcript(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def transcript():
    return load_transcript
