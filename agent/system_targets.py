"""조사 대상 확인과 계획 정규화. 미등록 이름을 다른 시스템으로 바꾸지 않습니다.

작성자: Git 이력 참조
작성목적: 모델 종류와 무관하게 명시된 지원 시스템만 조회하고 확인 대화를 이어갑니다.
작성일: 2026-10-20
카탈로그를 읽지 못하면 빈 목록으로 처리해 조회를 중단합니다.
"""
import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'monitoring.json'
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+')
RUNBOOK_DIR = CATALOG_PATH.parent / 'runbooks'
_URL = re.compile(r'https?://[^\s]+', re.I)
_INCIDENT = re.compile(r'장애|진단|다운|지연|급증|누수|타임아웃|incident|timeout', re.I)


def load_known_systems() -> set[str]:
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
        return set(catalog) if isinstance(catalog, dict) else set()
    except (OSError, ValueError):
        return set()


def mentioned_systems(query: str, known: set[str] | None = None) -> list[str]:
    known = load_known_systems() if known is None else known
    # URL 경로와 도메인, 런북 식별자는 조사 대상이 아닙니다.
    # 등록된 시스템 이름은 아래 카탈로그 검사에서 별도로 인식합니다.
    query = _URL.sub(lambda m: ' ' * len(m.group()), query)
    incident_types = {path.stem for path in RUNBOOK_DIR.glob('*.md')}
    mentions = [(m.start(), m.group().lower()) for m in _IDENTIFIER.finditer(query)
                if '.' not in m.group() and m.group().lower() not in incident_types]
    # 카탈로그에 하이픈 없는 식별자가 추가된 경우도 정확한 경계에서 인식합니다.
    for name in known:
        pattern = r'(?<![A-Za-z0-9_.-])' + re.escape(name) + r'(?![A-Za-z0-9_.-])'
        mentions.extend((m.start(), name) for m in re.finditer(pattern, query, re.I))
    return list(dict.fromkeys(name for _, name in sorted(mentions)))


def target_reply(query: str) -> bool:
    mentions = mentioned_systems(query)
    if not mentions:
        return False
    remainder = query
    for name in sorted(mentions, key=len, reverse=True):
        remainder = re.sub(re.escape(name), '', remainder, flags=re.I)
    remainder = re.sub(r'\s+', '', remainder)
    remainder = re.sub(r'그럼|그러면|맞아요|맞습니다|입니다|이에요|이요|예요|해줘요|해주세요|해줘|조사|진단|확인|조회|바꿔|변경|부탁|대상|시스템|그리고|및|네|으로|로|를|을|와|과|요|[,;\s.!?`]', '', remainder)
    return not remainder


def resume_query(query: str, pending: str | None) -> tuple[str, bool, bool]:
    """대상명만 답한 경우에만 원래 증상을 복원합니다. 새 요청과 잡담은 섞지 않습니다."""
    if not pending:
        return query, False, False
    if re.fullmatch(r'\s*(?:취소|그만|취소해줘|취소해주세요)[.!?\s]*', query):
        return query, False, True
    if not target_reply(query):
        return query, False, False
    known = load_known_systems()
    replacements = mentioned_systems(query, known)
    originals = mentioned_systems(pending, known)
    unknown = [name for name in originals if name not in known]
    if not unknown:
        return pending + '\n조회 대상: ' + ', '.join(replacements), True, False
    if len(unknown) == 1:
        replacement = ' 및 '.join(replacements)
        return re.sub(re.escape(unknown[0]), replacement, pending, flags=re.I), True, False
    if len(unknown) == len(replacements):
        mapping = dict(zip(unknown, replacements))
        pattern = '|'.join(re.escape(name) for name in sorted(unknown, key=len, reverse=True))
        return re.sub(pattern, lambda m: mapping[m.group().lower()], pending, flags=re.I), True, False
    return pending, True, False


def is_incident_request(query: str) -> bool:
    return bool(_INCIDENT.search(query) or (mentioned_systems(query) and re.search(
        r'상태|모니터링|점검|확인|CPU|메모리', query, re.I)))


def target_question(query: str) -> tuple[list[str], str | None]:
    known = load_known_systems()
    if not known:
        return [], '지원 시스템 목록을 읽을 수 없어 조회하지 않았습니다. 시스템 목록을 확인해주세요.'
    mentions = mentioned_systems(query, known)
    unknown = [name for name in mentions if name not in known]
    if unknown or not mentions:
        reason = f"지원 목록에 없는 시스템입니다: {', '.join(unknown)}.\n" if unknown else ''
        return [], reason + '조회할 시스템을 알려주세요.\n지원 시스템: ' + ', '.join(sorted(known))
    return mentions, None


def normalize_plan(raw: dict, query: str) -> dict:
    """모델의 누락·중복·추가와 무관하게 확인된 대상마다 모니터링 작업 하나를 만듭니다."""
    targets, question = target_question(query)
    raw = raw if isinstance(raw, dict) else {}
    tasks = [] if question else [
        {'id': f'task-monitoring-{i}', 'agent': 'research', 'kind': 'monitoring',
         'target': target, 'status': 'pending'} for i, target in enumerate(targets)]
    planned = raw.get('tasks', [])
    if not question and isinstance(planned, list) and any(
            isinstance(task, dict) and task.get('agent') == 'analysis' for task in planned):
        tasks.append({'id': 'task-analysis', 'agent': 'analysis', 'kind': 'pattern-analysis', 'status': 'pending'})
    return {'needs_analysis': bool(raw.get('needs_analysis', False)), 'tasks': tasks}
