"""Two ordered releases through the real managed CLI. Live execution is explicit."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PROFILES = [{'id': 'gpt-5.6-terra', 'effort': 'high'}, {'id': 'gpt-6-astra', 'effort': 'medium'}]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def setup(destination, runtime=ROOT):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Campaign destination must be new; preserve existing proof for explicit recovery')
    destination.mkdir(parents=True)
    repo = destination / 'project'; shutil.copytree(ROOT / 'fixtures/minimal', repo)
    remote = destination / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    git(repo, 'config', 'user.email', 'go-campaign@example.invalid'); git(repo, 'config', 'user.name', 'Go local campaign')
    git(repo, 'remote', 'add', 'origin', str(remote))
    version = subprocess.check_output([sys.executable, str(runtime / 'cli/go.py'), 'version'], text=True).strip()
    # CLI version is plain semver.
    project_path = repo / '.go/project.json'; project = json.loads(project_path.read_text())
    project.update(required_stack_version=version, stack_ref='v' + version)
    project['release_profiles'] = {'local': {'provider': 'git-tag', 'remote': 'origin', 'branch': 'main',
        'publication': {'version': {'path': 'VERSION', 'format': 'text'}, 'bump': 'minor', 'tag_prefix': 'v', 'changelog': 'CHANGELOG.md'}, 'deployment': {'mode': 'none', 'reason': 'Local Git release proof; no deployment target'}}}
    write(project_path, project)
    original = repo / '.go/tasks/open/task-schema-smoke.json'; seed = json.loads(original.read_text()); original.unlink()
    for index, profile in enumerate(PROFILES, 1):
        task = copy.deepcopy(seed); tid = 'campaign-' + str(index)
        expected = 'one\n' if index == 1 else 'two\n'
        intent = f'Write app.txt with exactly {expected!r}. This is a tiny local release fixture. Do not delegate or start subagents. Do not commit, push, tag, install dependencies, change VERSION/CHANGELOG, or mutate .go; the controller owns those actions. Read the supplied context and use the existing Python interpreter for verification. Critic: inspect the exact requested content, return the required protocol verdict and do not edit any file.'
        task.update(id=tid, summary=f'Deliver local campaign step {index}', description=intent,
            execution_mode='agent', shareable_delivery='none', order=index,
            scope={'read':['.go/**','app.txt','VERSION','CHANGELOG.md'], 'modify':['app.txt','VERSION','CHANGELOG.md']},
            acceptance=[f'app.txt contains {expected!r}; release 1.{index+1}.0 has verified local remote readback'],
            verification=[f"python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == {expected!r}; assert Path('VERSION').read_text().strip() == '1.{index+1}.0'\""],
            work_status='pending',review_status='none',outcome_tracking_version=1,
            intent_source={'text':intent,'sha256':hashlib.sha256(intent.encode()).hexdigest(),'source_ref':'abc-10 bounded campaign'},
            requested_outcomes=[{'id':'R1','text':f'Deliver step {index} with release proof','source':'campaign','status':'pending','evidence':[]}],
            execution_contract={'schema':'go-workflow.execution-contract.v1','task_kind':'product','model':profile,
                'release':{'mode':'required','profile':'local'},'workspace':{'mode':'task_worktree','base_branch':'main','control_state':'repo_local_single_writer'}})
        if index == 2: task['dependencies'] = [{'project':project['id'],'task_id':'campaign-1','requires':'done_with_required_release_evidence'}]
        write(repo / f'.go/tasks/open/{tid}.json',task)
    hierarchy = json.loads((repo / '.go/hierarchy.json').read_text())
    hierarchy['epics'][0]['features'][0]['tasks']=['campaign-1','campaign-2'];write(repo / '.go/hierarchy.json',hierarchy)
    (repo/'VERSION').write_text('1.1.0\n');(repo/'CHANGELOG.md').write_text('# Local campaign\n')
    (repo/'AGENTS.md').write_text('Local disposable two-task proof. Follow the task context. No subagents. Only app.txt is model-authored; the controller owns workflow state, versioning and publication. No external deployment.\n')
    git(repo,'add','.');git(repo,'commit','-qm','two explicit dependent campaign tasks')
    git(repo,'tag','-a','v1.1.0','-m','local baseline');git(repo,'push','origin','main','refs/tags/v1.1.0')
    return repo


def run_task(repo, task_id, *, runtime=ROOT, env=None, initial=True, commands=30, expected_blocked=False):
    workspace=repo.parent/(task_id+'-worker')
    args=[sys.executable,str(runtime/'cli/go.py'),'auto',str(repo),'--execute','--task-id',task_id,
          '--agent','campaign-owner','--executor-agent','codex','--max-commands',str(commands),
          '--max-minutes','8','--max-attempts','3','--command-timeout-seconds','180','--json']
    if initial:args+=['--workspace-path',str(workspace),'--workspace-branch','task/'+task_id,'--base-branch','main',
                     '--base-commit',git(repo,'rev-parse','HEAD'),'--run-id',task_id+'-run','--ship-policy','push','--allow-push']
    result=subprocess.run(args,cwd=repo,env=env,text=True,capture_output=True,timeout=540)
    proof=repo.parent/'invocations';proof.mkdir(exist_ok=True)
    number=len(list(proof.glob('*.json')))+1
    try: parsed=json.loads(result.stdout)
    except ValueError: parsed=None
    write(proof/f'{number:03d}.json',{'argv':args,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'result':parsed})
    if parsed is None or (result.returncode and not expected_blocked):raise RuntimeError(result.stdout+result.stderr)
    return parsed


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--destination',type=Path,required=True);parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--candidate-runtime',action='store_true',help='Explicit disposable-fixture development runtime; never a released-pin claim')
    args=parser.parse_args();runtime=args.runtime.resolve();env=dict(os.environ);env.pop('GO_STACK_ALLOW_DEV',None)
    env['PYTHONDONTWRITEBYTECODE']='1'
    if args.candidate_runtime:env['GO_STACK_ALLOW_DEV']='1'
    repo=setup(args.destination,runtime)
    runtime_files={str(p.relative_to(runtime)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ['go_workflow','schemas','cli'] for p in (runtime/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    write(repo.parent/'runtime-source.json',{'revision':git(runtime,'rev-parse','HEAD'),'files_sha256':runtime_files,'candidate_runtime':args.candidate_runtime,'campaign_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    write(repo.parent/'mode.json',{'mode':'live-native-codex','candidate_runtime':args.candidate_runtime,'profiles':PROFILES,'runtime':str(runtime),'effective_identity':'unconfirmed','remote':'local bare Git; no hosted publication/deployment'})
    for tid in ['campaign-1','campaign-2']:
        result=run_task(repo,tid,runtime=runtime,env=env)
        if tid not in result.get('completed_tasks',[]):
            write(repo.parent/'live-result.json',{'status':'blocked','task_id':tid,'result':result})
            print(json.dumps(result));return 1
    assert all(hashlib.sha256((runtime/name).read_bytes()).hexdigest()==digest for name,digest in runtime_files.items()),'Runtime source changed during live proof'
    write(repo.parent/'live-result.json',{'status':'passed','tasks':['campaign-1','campaign-2'],'profiles':PROFILES,'effective_identity':'unconfirmed','remote':'local bare Git; annotated tags verified by controller'})
    print('ABC_LIVE_CAMPAIGN_PASSED');return 0


if __name__=='__main__':raise SystemExit(main())
