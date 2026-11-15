"""Reviewed mapping of UI nodes to legacy steps and future analysis actions."""
from dataclasses import dataclass

from steps.ids import PIPELINE_STEP_NUMBERS


@dataclass(frozen=True)
class StageDefinition:
    node_id: str
    legacy_steps: tuple[int, ...]
    milestone: str
    reviews: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()


STAGES = (
    StageDefinition('collect', (0, 1, 2, 3), 'M2'),
    StageDefinition('keyword', (4, 5, 6, 7, 8), 'M2'),
    StageDefinition('market', (9, 10, 11, 12, 13, 14, 15), 'M2'),
    StageDefinition('attribute', (16, 17, 18), 'M3', ('attribute_spec', 'standard_vocabulary')),
    StageDefinition('voc', (19,), 'M3', actions=('extract_voc_evidence:M5',)),
    StageDefinition('opportunity', (), 'M5', actions=('discover_opportunities', 'compose_report_draft')),
    StageDefinition('quality', (20,), 'M3', actions=('validate_evidence_chain:M5',)),
    StageDefinition('report', (), 'M3', actions=('read_delivery_manifest',)),
)


def validate_mapping() -> None:
    numbers = tuple(n for stage in STAGES for n in stage.legacy_steps)
    if numbers != PIPELINE_STEP_NUMBERS:
        raise ValueError('Every legacy step must occur exactly once and in order.')
    if STAGES[-1].legacy_steps or STAGES[-1].actions != ('read_delivery_manifest',):
        raise ValueError('Delivery after final monitoring must be read-only.')
