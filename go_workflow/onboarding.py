"""Read-only project facts and explicit settings for existing Go intake paths."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .release import _version_change, relative_path, validate_publication

SCHEMA = 'go-workflow.onboarding-plan.v1'
FIELDS = {
    'model': 'Choose the model id and reasoning effort for product tasks.',
    'base_branch': 'Choose the branch from which task worktrees and releases start.',
    'verification': 'Choose real commands that verify the intended product result.',
    'publisher': 'Choose provider (git-tag or github-release), remote, and GitHub repository when applicable.',
    'version': 'Choose an existing static version source (path, format, and key for JSON/TOML).',
    'bump': 'Choose the semantic release increment: major, minor or patch.',
    'tag_prefix': 'Choose the release tag prefix, for example v.',
    'changelog': 'Choose the changelog path; release preparation may create it.',
    'deployment': 'Configure deployment explicitly, or select mode none with a reason.',
    'task': 'Describe the first task with id, summary, read/modify scope and acceptance criteria.',
}


class OnboardingError(ValueError):
    pass


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else None


def _source(repo, spec):
    if not isinstance(spec, dict) or not relative_path(spec.get('path')):
        raise OnboardingError('Version source must be an explicit repository-relative path')
    path = repo / spec['path']
    if path.is_symlink() or not path.resolve().is_relative_to(repo) or not path.is_file():
        raise OnboardingError('Create the chosen version file inside the project first: ' + spec['path'])
    try:
        text = path.read_bytes().decode('utf-8')
        current = _version_change(text, spec)
        if not isinstance(current, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', current):
            raise OnboardingError('Version source must contain static semantic X.Y.Z: ' + spec['path'])
        parts = current.split('.'); parts[-1] = str(int(parts[-1]) + 1)
        _version_change(text, spec, '.'.join(parts))  # require a writable static representation
        return current
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OnboardingError('Invalid version source ' + spec['path'] + ': ' + str(exc)) from exc


def project_facts(repo):
    versions, diagnostics, checks = [], [], []
    for spec in ({'path':'VERSION','format':'text'},
                 {'path':'package.json','format':'json','key':'version'},
                 {'path':'pyproject.toml','format':'toml','key':'project.version'},
                 {'path':'pyproject.toml','format':'toml','key':'tool.poetry.version'}):
        if not (repo / spec['path']).exists(): continue
        try: versions.append({**spec, 'current': _source(repo, spec)})
        except OnboardingError as exc: diagnostics.append(str(exc))
    makefile = repo / 'Makefile'
    if makefile.is_file() and not makefile.is_symlink():
        if re.search(r'^check\s*:', makefile.read_text(), re.M): checks.append('make check')
    package = repo / 'package.json'
    if package.is_file() and not package.is_symlink():
        try:
            if isinstance(json.loads(package.read_text()).get('scripts', {}).get('test'), str): checks.append('npm test')
        except (ValueError, AttributeError): pass
    valid_paths = {value['path'] for value in versions}
    diagnostics = [message for message in diagnostics if not any('Invalid version source ' + path + ':' in message for path in valid_paths)]
    return {'base_branch': _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD'),
            'remotes': (_git(repo, 'remote') or '').splitlines(),
            'version_sources': versions, 'verification_candidates': checks, 'diagnostics': diagnostics}


def plan_onboarding(repo, answers=None):
    repo = Path(repo).resolve()
    if not repo.is_dir(): raise OnboardingError('Inspect an existing project directory before onboarding')
    answers = {} if answers is None else deepcopy(answers)
    if not isinstance(answers, dict) or set(answers) - set(FIELDS) - {'critic_model'}:
        raise OnboardingError('Unknown onboarding answers or execution authority fields; settings grant no run authority')
    facts = project_facts(repo)
    candidates = {'base_branch': [facts['base_branch']] if facts['base_branch'] else [],
                  'version': [{k:v for k,v in value.items() if k!='current'} for value in facts['version_sources']],
                  'verification': facts['verification_candidates']}
    questions = [{'field':key, 'prompt':prompt, 'candidates':candidates.get(key, [])}
                 for key,prompt in FIELDS.items() if key not in answers]
    result = {'schema':SCHEMA, 'repo':str(repo), 'status':'needs_configuration', 'facts':facts,
              'questions':questions, 'settings':None, 'execution_brief':None}
    if questions: return result
    publisher = answers['publisher']; task = answers['task']
    if (not isinstance(publisher, dict) or set(publisher) - {'provider','remote','repository'}
            or not {'provider','remote'}.issubset(publisher)):
        raise OnboardingError('Publisher requires explicit provider/remote and optional GitHub repository')
    if (not isinstance(task, dict) or set(task) != {'id','summary','scope','acceptance'}
            or not isinstance(task.get('scope'), dict) or set(task['scope']) != {'read','modify'}
            or any(not isinstance(task['scope'][key],list) or not all(isinstance(p,str) and p.strip() for p in task['scope'][key]) for key in ('read','modify'))):
        raise OnboardingError('First task requires id, summary, read/modify scope and acceptance criteria')
    spec = {'version':answers['version'], 'bump':answers['bump'], 'tag_prefix':answers['tag_prefix'], 'changelog':answers['changelog']}
    errors = validate_publication(spec)
    if errors: raise OnboardingError('; '.join(errors))
    _source(repo, answers['version'])
    branch = answers['base_branch']
    if not isinstance(branch,str) or not branch.strip() or _git(repo,'check-ref-format','--branch',branch) != branch:
        raise OnboardingError('Choose an explicit valid base branch')
    settings = {'schema':'go-workflow.lifecycle-settings.v1',
        'execution_defaults': {'schema':'go-workflow.execution-contract.v1','task_kind':'product',
            'model':answers['model'],'release':{'mode':'required','profile':'project-release'},
            'workspace':{'mode':'task_worktree','control_state':'repo_local_single_writer','base_branch':branch}},
        'default_verification':answers['verification'],
        'release_profiles':{'project-release':{**publisher,'branch':branch,'publication':spec,'deployment':answers['deployment']}}}
    if 'critic_model' in answers: settings['execution_defaults']['critic_model'] = answers['critic_model']
    from .migrations import configured_project, MigrationError
    from .cli import FIXTURE_ROOT, validate_execution_brief
    try: configured_project(json.loads((FIXTURE_ROOT/'project.json').read_text()),settings)
    except MigrationError as exc: raise OnboardingError(str(exc)) from exc
    unit = {**task, 'execution_mode':'agent', 'shareable_delivery':'none', 'verification':answers['verification']}
    for key in ('read','modify'):
        unit['scope'][key] = list(dict.fromkeys(unit['scope'][key] + [answers['version']['path'], answers['changelog']]))
    approach = 'Deliver the explicitly described first task through the configured Go release lifecycle.'
    brief = {'schema':'go-workflow.execution-brief.v1','destination':task['summary'],
        'problem':task['summary'],'chosen_approach':approach,
        'non_goals':['No execution or remote-write authority is granted by onboarding settings.'],
        'source':{'recommendation':approach,'sha256':hashlib.sha256(approach.encode()).hexdigest(),'source_ref':'onboarding:explicit-answers'},
        'work_units':[unit]}
    errors = validate_execution_brief(brief)
    if errors: raise OnboardingError('; '.join(errors))
    result.update(status='ready',settings=settings,execution_brief=brief)
    return result
