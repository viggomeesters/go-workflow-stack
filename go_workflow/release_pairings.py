"""Immutable release baselines, separate from a starter's current runtime pin."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA = 'go-workflow.release-pairings.v1'
TEMPLATE_REPOSITORY = 'https://github.com/viggomeesters/go-project-template.git'
REF = re.compile(r'^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')
SHA = re.compile(r'^[0-9a-f]{40}$')
START = '<!-- go-stack-pairing:start -->'
END = '<!-- go-stack-pairing:end -->'


class PairingError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise PairingError('Duplicate manifest key: ' + key)
        result[key] = value
    return result


def load_manifest(path=None, *, expected_ref=None):
    if path is None:
        path = Path(__file__).resolve().parents[1] / 'release-pairings.json'
        if not path.is_file(): path = Path(sys.prefix) / 'release-pairings.json'
    try: data = json.loads(Path(path).read_text(), object_pairs_hook=_unique)
    except (OSError, ValueError) as exc: raise PairingError('Cannot read release pairing manifest: ' + str(exc)) from exc
    if (not isinstance(data,dict) or set(data) != {'schema','current_stack_ref','template_repository','pairings'}
            or data['schema'] != SCHEMA or data['template_repository'] != TEMPLATE_REPOSITORY
            or not isinstance(data['current_stack_ref'],str) or not REF.fullmatch(data['current_stack_ref'])
            or not isinstance(data['pairings'],list)):
        raise PairingError('Invalid release pairing manifest')
    seen = set()
    for pair in data['pairings']:
        if (not isinstance(pair,dict) or set(pair) != {'stack_ref','template_commit','template_ref','note'}
                or not isinstance(pair['stack_ref'],str) or not REF.fullmatch(pair['stack_ref'])
                or not isinstance(pair['note'],str) or not pair['note'].strip()):
            raise PairingError('Invalid release pairing entry')
        ref,commit,template_ref = pair['stack_ref'],pair['template_commit'],pair['template_ref']
        if ref in seen: raise PairingError('Duplicate stack pairing: ' + ref)
        seen.add(ref)
        if commit is not None and (not isinstance(commit,str) or not SHA.fullmatch(commit)):
            raise PairingError('Pairing requires an immutable template commit')
        if template_ref is not None and (commit is None or not isinstance(template_ref,str) or not REF.fullmatch(template_ref)):
            raise PairingError('Invalid template ref/commit pairing')
    current = pairing_for(data,data['current_stack_ref'])
    if current['template_commit'] is None: raise PairingError('Current release requires an immutable template baseline')
    if expected_ref is not None and data['current_stack_ref'] != expected_ref:
        raise PairingError('Manifest current stack ref contradicts runtime version: ' + str(expected_ref))
    return data


def pairing_for(data, ref):
    for pair in data['pairings']:
        if pair['stack_ref'] == ref: return dict(pair)
    raise PairingError('Release pairing is missing for ' + str(ref))


def _git(repo, *args):
    env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null',GIT_CONFIG_NOSYSTEM='1')
    result = subprocess.run(['git','--no-replace-objects','-c','core.fsmonitor=false','-C',str(repo),*args],
                            env=env,text=True,capture_output=True,timeout=30)
    if result.returncode: raise PairingError('Cannot verify Git pairing: ' + result.stderr.strip())
    return result.stdout.strip()


def _tag(repo, ref):
    if _git(repo,'cat-file','-t','refs/tags/'+ref) != 'tag':
        raise PairingError('Pairing requires an annotated immutable tag: ' + ref)
    return _git(repo,'rev-parse','refs/tags/'+ref+'^{commit}')


def verify_baseline(data, ref, template):
    pair = pairing_for(data,ref)
    if pair['template_commit'] is None: raise PairingError('No historical template baseline was recorded for ' + ref)
    actual = _git(template,'rev-parse','HEAD')
    if actual != pair['template_commit']: raise PairingError('Template baseline commit differs from the manifest')
    if pair['template_ref'] and _tag(template,pair['template_ref']) != actual:
        raise PairingError('Template tag and manifest commit disagree')
    if _git(template,'status','--porcelain','--untracked-files=all'):
        raise PairingError('Template baseline must use a clean immutable checkout')
    return {**pair,'verified':True}


def render_metadata(metadata):
    return '\n'.join([START,
        'Pinned Go stack: [`'+metadata['stack_ref']+'`](https://github.com/viggomeesters/go-workflow-stack/releases/tag/'+metadata['stack_ref']+').',
        'Runtime commit: `'+metadata['stack_commit']+'`.', END])


def current_template(data, template, stack_repo, *, check_docs=True):
    template = Path(template)
    try: project = json.loads((template/'.go/project.json').read_text())
    except (OSError,ValueError) as exc: raise PairingError('Cannot read template project pin') from exc
    if not isinstance(project,dict): raise PairingError('Template project pin must be an object')
    ref = project.get('stack_ref')
    if (not isinstance(ref,str) or not REF.fullmatch(ref) or project.get('required_stack_version') != ref[1:]
            or ref != data['current_stack_ref']):
        raise PairingError('Template pin must match both version fields and the current stack pairing')
    pair = pairing_for(data,ref); commit = _tag(stack_repo,ref)
    constants = _git(stack_repo,'show',commit+':go_workflow/constants.py')
    match = re.search(r'^STACK_VERSION = "([^"]+)"',constants,re.M)
    if not match or match.group(1) != ref[1:]: raise PairingError('Stack tag declares a contradictory version')
    result = {'stack_ref':ref,'stack_commit':commit,'baseline_template_commit':pair['template_commit'],
              'baseline_template_ref':pair['template_ref']}
    if check_docs:
        try: readme = (template/'README.md').read_text()
        except OSError as exc: raise PairingError('Template README is missing') from exc
        if readme.count(START)!=1 or readme.count(END)!=1:
            raise PairingError('Template README requires exactly one generated pairing block')
        block = readme[readme.index(START):readme.index(END)+len(END)]
        if block != render_metadata(result): raise PairingError('Template README pairing metadata is stale or contradictory')
    return result
