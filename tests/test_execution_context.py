"""Isolation proof for managed stack verification's public-template fixture."""
import json
from pathlib import Path

import pytest

from test_abc_context import workspace_fixture
from go_workflow.execution_context import git_state, verification_checkout, ContextError
from test_abc_worktrees import git


def test_verification_checkout_clones_template_sibling_without_mutating_source(tmp_path):
    control, workspace, _ = workspace_fixture(tmp_path)
    template = tmp_path / 'go-project-template'
    template.mkdir()
    git(template, 'init', '-q', '-b', 'main')
    git(template, 'config', 'user.name', 'Pytest')
    git(template, 'config', 'user.email', 'pytest@example.com')
    (template / '.go').mkdir()
    (template / '.go/project.json').write_text('{"id":"template"}')
    (template / 'scripts').mkdir()
    (template / 'scripts/bootstrap-stack.sh').write_text('#!/bin/bash\n')
    git(template, 'add', '.'); git(template, 'commit', '-qm', 'template fixture')
    original = git(template, 'rev-parse', 'HEAD')
    before = git(template, 'status', '--porcelain')
    record = {'path': str(workspace), 'control_repo': str(control), 'base_commit': git(workspace, 'rev-parse', 'HEAD')}
    code = git_state(workspace, record)
    with verification_checkout(workspace, record, code) as (candidate, proof):
        sibling = candidate.parent / 'go-project-template'
        assert sibling.is_dir()
        assert git(sibling, 'rev-parse', 'HEAD') == original
        assert (sibling / 'scripts/bootstrap-stack.sh').is_file()
        (sibling / 'scripts/bootstrap-stack.sh').write_text('modified only in disposable clone')
        assert proof['template_head'] == original
    assert git(template, 'rev-parse', 'HEAD') == original
    assert git(template, 'status', '--porcelain') == before


def test_verification_checkout_refuses_dirty_template_sibling(tmp_path):
    control, workspace, _ = workspace_fixture(tmp_path)
    template = tmp_path / 'go-project-template'
    template.mkdir();git(template, 'init', '-q', '-b', 'main')
    git(template, 'config', 'user.name', 'Pytest')
    git(template, 'config', 'user.email', 'pytest@example.com')
    (template / '.go').mkdir();(template / '.go/project.json').write_text('{"id":"template"}')
    git(template, 'add', '.');git(template, 'commit', '-qm', 'template fixture')
    (template / 'changed.txt').write_text('unowned')
    record = {'path': str(workspace), 'control_repo': str(control), 'base_commit': git(workspace, 'rev-parse', 'HEAD')}
    with pytest.raises(ContextError, match='template.*dirty'):
        with verification_checkout(workspace, record, git_state(workspace, record)):
            pass
