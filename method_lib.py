import json
import os
import time
import uuid
import re
from typing import Any
from memory.memory_system import LongTermMemory

class MethodologyLibrary:
    CATEGORY_RULES = [
        ("数据分析/报表", ["分析", "报表", "指标", "趋势", "sql", "tableau", "bi", "data"]),
        ("开发工程/代码实现", ["代码", "开发", "接口", "api", "bug", "python", "java", "前端", "后端"]),
        ("产品设计/需求", ["需求", "prd", "原型", "交互", "产品", "用户故事", "roadmap"]),
        ("运营增长/市场", ["运营", "投放", "转化", "增长", "市场", "活动", "拉新"]),
        ("文案内容/知识整理", ["文案", "写作", "总结", "整理", "提纲", "说明书", "知识库"]),
    ]

    def __init__(self, file_path="data/methodology/methodologies.json"):
        self.file_path = file_path
        self.feedback_file = "data/methodology/category_feedback.json"
        self._ltm = LongTermMemory()
        self._ltm.load()
        # 确保目录存在
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        # 确保文件存在
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        if not os.path.exists(self.feedback_file):
            with open(self.feedback_file, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)

    # 加载方法论库
    def load_methodologies(self):
        self._ltm.load()
        return [self.normalize_methodology(m) for m in self._ltm.get_all_methodologies()]

    # 保存方法论库
    def save_methodologies(self, methodologies):
        self._ltm.methodologies = [self.normalize_methodology(m) for m in (methodologies or []) if isinstance(m, dict)]
        self._ltm.save()

    def _load_category_feedback(self):
        try:
            with open(self.feedback_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_category_feedback(self, feedback_map: dict):
        with open(self.feedback_file, "w", encoding="utf-8") as f:
            json.dump(feedback_map, f, ensure_ascii=False, indent=2)

    def _normalize_keywords(self, keywords: Any):
        if keywords is None:
            return []
        if isinstance(keywords, list):
            return [str(k).strip() for k in keywords if str(k).strip()]
        if isinstance(keywords, str):
            import re
            parts = re.split(r"[\s,，;；/|]+", keywords.strip())
            return [p for p in parts if p]
        return [str(keywords).strip()] if str(keywords).strip() else []

    def _normalize_scene(self, scene: str):
        scene = (scene or "").strip().lower()
        scene = re.sub(r"[^\w\u4e00-\u9fff]+", " ", scene)
        scene = re.sub(r"\s+", " ", scene).strip()
        return scene

    def _normalize_steps(self, steps: Any):
        if steps is None:
            return []
        if isinstance(steps, list):
            return [str(s).strip() for s in steps if str(s).strip()]
        if isinstance(steps, str):
            import re
            return [s.strip() for s in re.split(r"[\n;；]+", steps) if s.strip()]
        return [str(steps).strip()] if str(steps).strip() else []

    def _pick_category(self, text: str):
        text = (text or "").lower()
        greeting_keywords = ["你好", "您好", "hello", "hi", "在吗", "谢谢", "早上好", "晚上好"]
        if any(g in text for g in greeting_keywords):
            # 问候/寒暄默认归入通用，避免误分到需求类
            return "通用/其他"
        best_cat = "通用/其他"
        best_score = 0
        for cat, kws in self.CATEGORY_RULES:
            score = sum(1 for kw in kws if kw in text)
            if score > best_score:
                best_score = score
                best_cat = cat
        # 至少命中2个领域关键词才归入专业分类，降低误判
        if best_score >= 2:
            return best_cat
        return "通用/其他"

    def _build_event_key(self, scene: str, keywords: list[str]):
        scene_norm = self._normalize_scene(scene)
        kw = sorted({k.lower().strip() for k in (keywords or []) if k and str(k).strip()})
        head = kw[:4]
        return f"{scene_norm}|{'/'.join(head)}" if (scene_norm or head) else ""

    def _find_similar_methodology(self, methodologies: list[dict], new_method: dict):
        new_key = new_method.get("event_key", "")
        new_scene = self._normalize_scene(new_method.get("scene", ""))
        new_kw = set((new_method.get("keywords") or []))
        for idx, m in enumerate(methodologies):
            if m.get("event_key") and m.get("event_key") == new_key:
                return idx
            m_scene = self._normalize_scene(m.get("scene", ""))
            m_kw = set((m.get("keywords") or []))
            if new_scene and m_scene == new_scene:
                inter = len(new_kw & m_kw)
                union = len(new_kw | m_kw) or 1
                if inter / union >= 0.6:
                    return idx
        return -1

    def normalize_methodology(self, method: dict):
        method = dict(method)
        method_id = method.get("method_id") or method.get("id") or str(uuid.uuid4())
        scene = method.get("scene") or method.get("scenario") or ""
        title = method.get("title") or method.get("name") or (scene[:24] if scene else f"方法论-{method_id[:8]}")
        keywords = self._normalize_keywords(method.get("keywords") or method.get("core_keywords"))
        solve_steps = self._normalize_steps(method.get("solve_steps") or method.get("steps"))
        applicable_range = method.get("applicable_range") or method.get("applicableRange") or "通用"
        event_key = method.get("event_key") or self._build_event_key(scene, keywords)
        feedback = self._load_category_feedback()
        feedback_category = feedback.get(event_key, "")
        category = method.get("category") or feedback_category or self._pick_category(" ".join([title, scene, " ".join(keywords)]))
        created_at = method.get("created_at") or method.get("create_time") or time.strftime("%Y-%m-%d %H:%M:%S")
        updated_at = method.get("updated_at") or method.get("last_update_time") or created_at
        try:
            version = max(1, int(method.get("version", 1)))
        except Exception:
            version = 1
        try:
            score = float(method.get("score", 0.0))
        except Exception:
            score = 0.0
        quality_metrics = method.get("quality_metrics") if isinstance(method.get("quality_metrics"), dict) else {}
        evidence_refs = method.get("evidence_refs") if isinstance(method.get("evidence_refs"), list) else []
        version_history = method.get("version_history") if isinstance(method.get("version_history"), list) else []
        retrieval_meta = method.get("retrieval_meta") if isinstance(method.get("retrieval_meta"), dict) else {}
        return {
            "method_id": method_id,
            "title": title,
            "category": category,
            "scene": scene,
            "keywords": keywords,
            "solve_steps": solve_steps,
            "applicable_range": applicable_range,
            "status": method.get("status", "published"),
            "usage_count": int(method.get("usage_count", 0)),
            "success_count": int(method.get("success_count", 0)),
            "version": version,
            "parent_version": method.get("parent_version"),
            "rollback_to": method.get("rollback_to"),
            "score": max(0.0, min(1.0, score)),
            "quality_metrics": quality_metrics,
            "evidence_refs": evidence_refs,
            "version_history": version_history,
            "retrieval_meta": retrieval_meta,
            "event_key": event_key,
            "create_time": created_at,
            "updated_at": updated_at,
        }

    # 添加方法论
    def add_methodology(self, scene, keywords=None, solve_steps=None):
        # 兼容旧接口 add_methodology(scene, keywords, solve_steps)
        if isinstance(scene, dict):
            payload = dict(scene)
        else:
            payload = {
                "method_id": str(uuid.uuid4()),
                "title": (str(scene or "")[:24] if scene else "新方法论"),
                "category": self._pick_category(str(scene or "")),
                "scene": scene,
                "keywords": keywords or [],
                "solve_steps": solve_steps or [],
                "status": "published",
                "usage_count": 0,
                "success_count": 0,
                "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        normalized = self.normalize_methodology(payload)
        self._ltm.load()
        return self._ltm.add_methodology(normalized)

    # 查找方法论
    def search_methodologies(self, query):
        self._ltm.load()
        q = (query or "").strip()
        if not q:
            return self._ltm.get_all_methodologies()
        return self._ltm.search_methodologies(q)

    # 获取所有方法论
    def get_all_methodologies(self):
        self._ltm.load()
        return self._ltm.get_all_methodologies()

    # 删除方法论
    def delete_methodology(self, method_id):
        self._ltm.load()
        return self._ltm.delete_methodology(method_id)

    def delete_methodologies_batch(self, method_ids: list[str]):
        self._ltm.load()
        return self._ltm.delete_methodologies_batch(method_ids)

    def update_methodology_category(self, method_id: str, category: str):
        self._ltm.load()
        updated = self._ltm.update_methodology_category(method_id, category)
        if updated:
            feedback = self._load_category_feedback()
            event_key = updated.get("event_key", "")
            if event_key:
                feedback[event_key] = updated.get("category", "通用/其他")
                self._save_category_feedback(feedback)
        return updated

    # 根据ID获取方法论
    def get_methodology_by_id(self, method_id):
        self._ltm.load()
        return self._ltm.get_methodology_by_id(method_id)

    def rollback_methodology(self, method_id: str, to_version: int):
        self._ltm.load()
        return self._ltm.rollback_methodology(method_id, to_version)

    def get_methodology_health_dashboard(self, limit: int = 100):
        self._ltm.load()
        return self._ltm.methodology_health_dashboard(limit=limit)

    def get_ab_stats_summary(self):
        self._ltm.load()
        stats = self._ltm.ab_stats if isinstance(self._ltm.ab_stats, dict) else {}
        pairs = stats.get("pairs") if isinstance(stats.get("pairs"), dict) else {}
        methods = stats.get("methods") if isinstance(stats.get("methods"), dict) else {}
        return {
            "pair_count": len(pairs),
            "method_count": len(methods),
            "updated_at": stats.get("updated_at", ""),
        }
