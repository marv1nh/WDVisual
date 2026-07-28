from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ProgressCallback = Callable[[int], None]


@dataclass(frozen=True)
class AnalysisContext:
    scope: dict[str, Any]
    input_revision: str
    capture_path: Path
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ModuleResult:
    output: dict[str, Any]
    confidence: dict[str, Any]
    warnings: list[str]


class AnalysisModule:
    name = ""
    version = ""
    required_tables: tuple[str, ...] = ()
    description = ""

    def validate(self, connection) -> list[str]:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(self.required_tables) - tables)
        return [f"Required table is missing: {name}" for name in missing]

    def run(
        self,
        connection,
        context: AnalysisContext,
        progress: ProgressCallback,
    ) -> ModuleResult:
        raise NotImplementedError

