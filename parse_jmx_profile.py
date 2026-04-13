import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_sampler_filter_config(config_path: Optional[Path] = None) -> List[str]:
    """
    Загружает конфигурацию фильтра Samplers из файла sampler_filter.json.
    Возвращает список префиксов имен Samplers, которые должны учитываться.
    По умолчанию: ["HTTP"]
    """
    default_prefixes = ["HTTP"]
    
    if config_path is None:
        # Пытаемся найти конфиг рядом со скриптом
        script_dir = Path(__file__).parent
        config_path = script_dir / "sampler_filter.json"
    
    if config_path and config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                prefixes = config.get("allowed_sampler_prefixes", default_prefixes)
                if isinstance(prefixes, list):
                    return [str(p) for p in prefixes if p]
        except Exception as e:
            print(f"Предупреждение: не удалось загрузить конфиг фильтра из {config_path}: {e}")
            print("Используются значения по умолчанию: ['HTTP']")
    
    return default_prefixes


def is_sampler_allowed(sampler_name: str, allowed_prefixes: List[str]) -> bool:
    """
    Проверяет, должен ли Sampler учитываться на основе его имени.
    
    Args:
        sampler_name: Имя Sampler
        allowed_prefixes: Список префиксов, которые разрешены
    
    Returns:
        True если Sampler должен учитываться, False иначе
    """
    if not sampler_name or not allowed_prefixes:
        return False
    
    for prefix in allowed_prefixes:
        if sampler_name.startswith(prefix):
            return True
    
    return False


def get_first_child_text(elem: ET.Element, tag_name: str) -> Optional[str]:
    """Return text of the first direct child with the given tag name."""
    for child in elem:
        if child.tag == tag_name:
            return child.text
    return None


def parse_ultimatethreadgroup(utg_elem: ET.Element) -> Dict[str, Any]:
    """
    Extract stages (only enabled UTG).

    Для каждой строки Ultimate Thread Group (threads, init_delay, ramp_up, hold, ramp_down):
    - plateau_start_s = init_delay + ramp_up — начало устойчивого плато (после ramp-up);
    - plateau_end_s = plateau_start_s + hold — конец плато; длительность сравнения = только hold.
    Промежутки ramp-up/ramp-down и «стартап» между ступенями в [plateau_start_s, plateau_end_s) не входят:
    отчёт и StageTracker сравнивают метрики по чистому плато.
    Ступени нумеруются с 1 (первая строка UTG → stage_idx=1).
    """
    utg_name = utg_elem.attrib.get("testname", "")
    data_root = utg_elem.find(".//collectionProp[@name='ultimatethreadgroupdata']")
    stages: List[Dict[str, Any]] = []

    if data_root is not None:
        for idx, row in enumerate(data_root.findall("collectionProp")):
            # Order of stringProp values is positional: threads, init_delay, ramp_up, hold, ramp_down
            values = [sp.text or "" for sp in row.findall("stringProp")]
            if len(values) < 5:
                # Skip malformed rows
                continue
            try:
                threads = int(values[0])
                init_delay = int(values[1])
                ramp_up = int(values[2])
                hold = int(values[3])
                ramp_down = int(values[4])
            except ValueError:
                continue

            plateau_start = init_delay + ramp_up
            plateau_end = plateau_start + hold
            stages.append(
                {
                    "stage_idx": idx + 1,
                    "threads": threads,
                    "init_delay_s": init_delay,
                    "ramp_up_s": ramp_up,
                    "hold_s": hold,
                    "ramp_down_s": ramp_down,
                    "plateau_start_s": plateau_start,
                    "plateau_end_s": plateau_end,
                }
            )

    return {"name": utg_name, "stages": stages}


def find_first_ctt_throughput(hash_tree: ET.Element) -> Optional[float]:
    """
    Find the first ConstantThroughputTimer under the given hashTree.
    
    ВАЖНО: Constant Throughput Timer в JMeter работает в запросах в минуту (RPM),
    а не в секунду.
    
    Возвращаем значение как RPM (не конвертируем), так как target_rps нужно
    вычислять для каждой ступени отдельно с учетом количества потоков:
    - Для calcMode=0: target_rps = (ctt_rpm * threads) / 60.0
    - Для calcMode=1 или 2: target_rps = ctt_rpm / 60.0
    
    Returns:
        Throughput в запросах в минуту (RPM), или None если не найден
    """
    for elem in hash_tree.iter("ConstantThroughputTimer"):
        for dp in elem.findall("doubleProp"):
            name = get_first_child_text(dp, "name")
            if name == "throughput":
                val_text = get_first_child_text(dp, "value")
                if val_text:
                    try:
                        rpm = float(val_text)  # CTT значение в запросах в минуту
                        return rpm  # Возвращаем как RPM, конвертация будет в send_profile_to_influx.py
                    except ValueError:
                        return None
    return None


def collect_samplers(root: ET.Element, allowed_prefixes: List[str] = None) -> List[Dict[str, Any]]:
    """
    Collect enabled samplers of any type.
    Учитываются только Samplers, которые начинаются с одного из разрешенных префиксов.
    
    Args:
        root: Корневой элемент XML
        allowed_prefixes: Список префиксов имен Samplers, которые должны учитываться.
                          По умолчанию: ["HTTP"]
    """
    if allowed_prefixes is None:
        allowed_prefixes = load_sampler_filter_config()
    
    samplers: List[Dict[str, Any]] = []
    for elem in root.iter():
        # JMeter samplers usually end with 'SamplerProxy' or JDBC Sampler types
        if not elem.tag.endswith("SamplerProxy") and "Sampler" not in elem.tag:
            continue
        if elem.attrib.get("enabled", "true") != "true":
            continue

        sampler_type = elem.tag
        name = elem.attrib.get("testname", "")
        
        # Учитываем только Samplers, которые начинаются с одного из разрешенных префиксов
        if not is_sampler_allowed(name, allowed_prefixes):
            continue
        
        path = get_first_child_text(elem, "stringProp")
        # Try to find specific path properties
        for sp in elem.findall("stringProp"):
            prop_name = sp.attrib.get("name", "")
            if prop_name in ("HTTPSampler.path", "query", "JDBCSampler.query"):
                path = sp.text
                break

        sampler_info = {
            "name": name,
            "type": sampler_type,
            "path_or_query": path or "",
            "max_response_time_ms": 10000,  # Значение по умолчанию: 10 секунд
        }
        samplers.append(sampler_info)
    return samplers


def find_all_transaction_controllers_in_hash_tree(hash_tree: ET.Element) -> List[str]:
    """
    Рекурсивно находит все Transaction Controllers внутри hashTree.
    Возвращает список имен всех найденных Transaction Controllers.
    """
    transaction_names = []
    
    def walk_recursive(elem: ET.Element):
        """Рекурсивно обходит элементы и их hashTree."""
        children = list(elem)
        i = 0
        while i < len(children):
            comp = children[i]
            sibling_hash = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "hashTree" else None
            
            # Проверяем, является ли элемент Transaction Controller
            if comp.tag == "TransactionController" and comp.attrib.get("enabled", "true") == "true":
                tc_name = comp.attrib.get("testname", "")
                if tc_name:
                    transaction_names.append(tc_name)
            
            # Рекурсивно обходим hashTree, если он есть
            if sibling_hash is not None:
                walk_recursive(sibling_hash)
            
            i += 2  # Пропускаем элемент и его hashTree
    
    walk_recursive(hash_tree)
    return transaction_names


def walk_hash_tree_and_collect(root_hash: ET.Element) -> List[Dict[str, Any]]:
    """
    Walk any hashTree recursively; in JMeter structure components and their hashTree siblings
    alternate. Collect UTGs with their sibling hashTree.
    """
    found: List[Dict[str, Any]] = []

    children = list(root_hash)
    i = 0
    while i < len(children):
        comp = children[i]
        sibling_hash = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "hashTree" else None

        if comp.tag == "kg.apc.jmeter.threads.UltimateThreadGroup" and comp.attrib.get("enabled", "true") == "true":
            utg_info = parse_ultimatethreadgroup(comp)
            
            # Находим все Transaction Controllers внутри этой Thread Group
            transaction_names = []
            if sibling_hash is not None:
                transaction_names = find_all_transaction_controllers_in_hash_tree(sibling_hash)
            
            # Если транзакций не найдено, добавляем имя Thread Group для обратной совместимости
            utg_name = utg_info.get("name", "")
            if not transaction_names and utg_name:
                # Добавляем имя Thread Group и с подчеркиванием для обратной совместимости
                transaction_names = [utg_name, f"_{utg_name}"]
            elif transaction_names and utg_name:
                # Также добавляем имя Thread Group для обратной совместимости (если его еще нет)
                if utg_name not in transaction_names:
                    transaction_names.insert(0, utg_name)
                if f"_{utg_name}" not in transaction_names:
                    transaction_names.insert(1, f"_{utg_name}")
            
            # Добавляем список транзакций к информации о Thread Group
            utg_info["transaction_names"] = transaction_names
            ctt = find_first_ctt_throughput(sibling_hash) if sibling_hash is not None else None
            utg_info["target_rps"] = ctt
            found.append(utg_info)

        # Recurse into hashTree sibling to find nested components as well
        if sibling_hash is not None:
            found.extend(walk_hash_tree_and_collect(sibling_hash))

        i += 2 if sibling_hash is not None else 1

    return found


def parse_jmx(jmx_path: Path) -> Dict[str, Any]:
    tree = ET.parse(jmx_path)
    root = tree.getroot()

    profile: Dict[str, Any] = {
        "test_name": jmx_path.stem,
        "thread_groups": [],
        "samplers": [],
    }

    top_hash = root.find("hashTree")
    if top_hash is not None:
        profile["thread_groups"] = walk_hash_tree_and_collect(top_hash)

    profile["samplers"] = collect_samplers(root)
    return profile


def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python parse_jmx_profile.py <input.jmx> [output.json]")
        sys.exit(1)

    input_path = Path(argv[1])
    output_path = Path(argv[2]) if len(argv) > 2 else input_path.with_suffix(".profile.json")

    data = parse_jmx(input_path)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote profile to {output_path}")


if __name__ == "__main__":
    main(sys.argv)
