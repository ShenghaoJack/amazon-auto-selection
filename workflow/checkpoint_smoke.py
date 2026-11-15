"""Offline dependency smoke: two isolated graph threads, persisted across processes.

This graph uses stub functions only. It never imports or runs business steps.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict


class SmokeState(TypedDict):
    run_id: str
    standardized: bool


def build_graph(directory: Path, saver):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def prepare(state: SmokeState):
        marker = directory / (state['run_id'] + '.count')
        count = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(count + 1))
        return {'standardized': False}

    def review(state: SmokeState):
        answer = interrupt({'run_id': state['run_id'], 'revision': 7})
        if answer != {'revision': 7, 'approved': True}:
            raise ValueError('Unexpected smoke review response')
        return {}

    def standardize(_state: SmokeState):
        return {'standardized': True}

    builder = StateGraph(SmokeState)
    builder.add_node('prepare', prepare)
    builder.add_node('review', review)
    builder.add_node('standardize', standardize)
    builder.add_edge(START, 'prepare')
    builder.add_edge('prepare', 'review')
    builder.add_edge('review', 'standardize')
    builder.add_edge('standardize', END)
    return builder.compile(checkpointer=saver)


def main() -> int:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--phase', choices=['prepare', 'resume'], required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    database = str(args.directory / 'checkpoints.sqlite')
    with SqliteSaver.from_conn_string(database) as saver:
        saver.setup()

    def execute(run_id: str):
        with SqliteSaver.from_conn_string(database) as saver:
            graph = build_graph(args.directory, saver)
            config = {'configurable': {'thread_id': run_id}}
            if args.phase == 'prepare':
                result = graph.invoke({'run_id': run_id, 'standardized': False}, config)
                assert result.get('__interrupt__') and not result['standardized']
                assert graph.get_state(config).next == ('review',)
            else:
                before = graph.get_state(config)
                assert before.values['run_id'] == run_id and before.next == ('review',)
                result = graph.invoke(Command(resume={'revision': 7, 'approved': True}), config)
                assert result['run_id'] == run_id and result['standardized'] is True
                assert not graph.get_state(config).next
            assert (args.directory / (run_id + '.count')).read_text() == '1'
            return {'run_id': run_id, 'phase': args.phase, 'prepared_calls': 1, 'standardized': result['standardized']}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ['m0-thread-a', 'm0-thread-b']))
    print(json.dumps({'results': results}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
