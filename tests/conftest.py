import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(relative_path: str) -> dict[str, Any]:
        return json.loads((FIXTURES / relative_path).read_text())

    return _load
