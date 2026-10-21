"""수집 근거에 대한 제한적 답변 검사와 검수 실패 시 사실 요약.

작성자: Git 이력 참조
작성목적: 수치 조작, 배포 원인 미확인 롤백, CPU 추세 단정을 줄입니다.
작성일: 2026-10-20
자연어 전체의 사실성을 판정하지 않으며, 조치 실행 권한을 부여하지 않습니다.
"""
import re


WRITING_RULES = (
    "당신은 ICT 운영 보고 작성자다. 수집된 사실만 사용해 한국어 습니다체로 "
    "간결한 대응 보고를 작성하라. 반드시 '1) ... 2) ... 3) ...' 번호 절차로 답하라. "
    "관측 수치는 원래 시스템과 지표에 맞게 그대로 인용하고, 없는 수치나 임계값을 만들지 마라. "
    "답변 첫 항목에는 조사한 시스템과 수집된 알림을 요약하라. "
    "확인하지 않은 원인을 단정하지 말고 추가 점검과 설정 변경은 조건부로 제안하라. "
    "타임아웃 값 상향은 짧은 설정 때문에 정상 응답이 실패한 것이 확인된 경우에만 제안하라. "
    "사용자가 제시한 증상은 관측 사실과 구분하라. CPU 런북을 참고했다면 "
    "현재 측정값과 함께 '현재 측정값만으로 CPU 급증 추세를 확정할 수 없습니다'라고 명시하고 "
    "과거 지표와 배포 시점 확인을 제안하라. "
    "런북은 참고 후보다. 관련 조치만 원문의 조건을 유지해 제안하라. "
    "롤백은 반드시 같은 문장에서 '배포가 원인으로 확인되면'이라는 조건을 붙여라. "
    "메모리 상한선을 높이면 누수가 해결된다고 단정하지 마라. "
    "여러 런북의 모든 조치를 나열하거나, 제안된 조치를 실행했다고 말하지 마라."
)
CPU_UNCERTAINTY = "현재 측정값만으로 CPU 급증 추세를 확정할 수 없습니다"
_PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[%％]")


def _source_percentages(collected: list[dict]) -> set[float]:
    supported = set()
    for entry in collected:
        if entry["source"] == "monitoring":
            monitoring = entry["data"]
            for metric in ("cpu_percent", "memory_percent"):
                if isinstance(monitoring.get(metric), (int, float)):
                    supported.add(float(monitoring[metric]))
            text = " ".join(monitoring.get("alerts", []))
        elif entry["source"] == "runbook":
            text = entry["data"]
        else:
            # LLM이 작성한 분석 문장을 독립 근거로 인정하지 않습니다.
            continue
        supported.update(float(number) for number in _PERCENT.findall(text))
    return supported


def needs_cpu_caveat(collected: list[dict]) -> bool:
    return (any(entry["source"] == "monitoring" for entry in collected)
            and any(entry["source"] == "runbook" and entry.get("incident_type") == "cpu-spike"
                    for entry in collected))


def grounding_issues(draft: str, collected: list[dict]) -> list[str]:
    """명시적인 수치와 조치 조건을 검사하며 범용 의미 검증을 대체하지 않습니다."""
    issues = []
    if len(draft) < 30 or "1)" not in draft:
        issues.append("30자 이상의 답변을 1) 2) 3) 번호 절차로 작성하세요.")
    supported = _source_percentages(collected)
    unsupported = sorted({float(number) for number in _PERCENT.findall(draft)} - supported)
    if unsupported:
        issues.append(f"근거에 없는 백분율 {unsupported}을 삭제하고 수집 수치만 인용하세요.")
    # 줄과 문장 단위로 검사해 다른 조치에 붙은 조건을 롤백 조건으로 오인하지 않습니다.
    sentences = re.split(r"[\n。!?]|(?<!\d)\.(?!\d)", draft.replace("**", ""))
    observations = [entry['data'] for entry in collected if entry['source'] == 'monitoring']
    for sentence in sentences:
        named = [obs for obs in observations if obs['system_name'] in sentence]
        candidates = named or observations
        # 지표명 바로 뒤의 수치만 검사합니다. 임계값 비교와 자연어 전체의 의미 판정은 구분합니다.
        for match in re.finditer(r'(CPU|메모리)(?:\s*사용률)?(?:은|는|이|가|:)?\s*(\d+(?:\.\d+)?)\s*[%％]', sentence, re.I):
            if re.match(r'\s*(?:이상|이하|초과|미만|를\s*(?:넘|초과))', sentence[match.end():]):
                continue
            metric = 'cpu_percent' if match[1].lower() == 'cpu' else 'memory_percent'
            values = {float(obs[metric]) for obs in candidates if isinstance(obs.get(metric), (int,float))}
            if values and float(match[2]) not in values:
                issues.append(f'{match[1]} 지표의 관측값과 인용 수치가 다릅니다. 해당 지표의 원래 수치를 사용하세요.')
        if re.search(r'타임아웃|timeout', sentence, re.I) and re.search(r'상향|늘리|올리', sentence):
            negated = re.search(r'(?:상향|늘리|올리).*?(?:않|금지|보류)', sentence)
            conditional = ('정상 응답' in sentence and '짧' in sentence
                           and re.search(r'확인되면|확인된 경우|경우에만', sentence))
            if not negated and not conditional:
                issues.append('타임아웃 상향은 설정이 짧아 정상 응답이 실패한 것이 확인된 경우에만 제안하세요.')
        if "롤백" not in sentence:
            continue
        if re.search(r"롤백(?:을|은|하지|을 하지)?\s*(?:하지 않|않|금지|보류)", sentence):
            continue
        if "배포가 원인으로 확인되면" not in sentence:
            issues.append("롤백 문장에는 '배포가 원인으로 확인되면'이라는 조건을 명시하세요.")
            break
    if needs_cpu_caveat(collected) and CPU_UNCERTAINTY not in draft:
        issues.append(f"CPU 관측과 요청 증상을 구분해 '{CPU_UNCERTAINTY}'를 명시하세요.")
    return issues


def evidence_only_answer(collected: list[dict]) -> str:
    """세 차례 반려된 생성 문장을 제외하고 도구 관측값만 제공합니다."""
    lines = ["1) 답변 검수를 통과하지 못해 수집된 사실만 제공합니다."]
    for entry in collected:
        if entry["source"] != "monitoring":
            continue
        monitoring = entry["data"]
        lines.append(f"관측: {monitoring['system_name']}, 상태 {monitoring['status']}, "
                     f"CPU {monitoring['cpu_percent']}%, 메모리 {monitoring['memory_percent']}%입니다.")
    if needs_cpu_caveat(collected):
        lines.append(CPU_UNCERTAINTY + ". 과거 지표와 배포 이력 확인이 필요합니다.")
    lines.extend(["2) 원인과 조치 조건은 추가 확인이 필요하며, 재시작이나 롤백을 실행하지 않았습니다.",
                  "3) 참고 런북의 조건을 확인한 뒤 담당자가 대응 여부를 판단해야 합니다."])
    return "\n".join(lines)
