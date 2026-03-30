import json
import os
import time
import uuid
import re
from typing import Any

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
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if not isinstance(raw, list):
                    return []
                return [self.normalize_methodology(m) for m in raw if isinstance(m, dict)]
        except Exception:
            return []

    # 保存方法论库
    def save_methodologies(self, methodologies):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(methodologies, f, ensure_ascii=False, indent=2)

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
            "event_key": event_key,
            "create_time": created_at,
            "updated_at": updated_at,
        }

    # 添加方法论
    def add_methodology(self, scene, keywords, solve_steps):
        methodologies = self.load_methodologies()
        new_methodology = self.normalize_methodology({
            "method_id": str(uuid.uuid4()),
            "title": scene[:24] if scene else "新方法论",
            "category": self._pick_category(scene or ""),
            "scene": scene,
            "keywords": keywords,
            "solve_steps": solve_steps,
            "status": "published",
            "usage_count": 0,
            "success_count": 0,
            "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        same_idx = self._find_similar_methodology(methodologies, new_methodology)
        if same_idx >= 0:
            existing = methodologies[same_idx]
            existing["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            existing["usage_count"] = int(existing.get("usage_count", 0)) + 1
            existing["success_count"] = max(int(existing.get("success_count", 0)), int(new_methodology.get("success_count", 0)))
            # 同事件保留更清晰标题，但避免频繁抖动
            if len(new_methodology.get("title", "")) > len(existing.get("title", "")):
                existing["title"] = new_methodology.get("title", existing.get("title"))
            existing["category"] = self._pick_category(" ".join([
                existing.get("title", ""),
                existing.get("scene", ""),
                " ".join(existing.get("keywords", [])),
            ]))
            methodologies[same_idx] = self.normalize_methodology(existing)
            self.save_methodologies(methodologies)
            return methodologies[same_idx]
        methodologies.append(new_methodology)
        self.save_methodologies(methodologies)
        return new_methodology

    # 查找方法论
    def search_methodologies(self, query):
        methodologies = self.load_methodologies()
        results = []
        q = (query or "").lower().strip()
        for method in methodologies:
            haystack = " ".join([
                method.get("title", ""),
                method.get("category", ""),
                method.get("scene", ""),
                " ".join(method.get("keywords", [])),
                " ".join(method.get("solve_steps", [])),
            ]).lower()
            if (not q) or (q in haystack):
                results.append(method)
        return results

    # 获取所有方法论
    def get_all_methodologies(self):
        return self.load_methodologies()

    # 删除方法论
    def delete_methodology(self, method_id):
        methodologies = self.load_methodologies()
        new_methodologies = [method for method in methodologies if method["method_id"] != method_id]
        if len(new_methodologies) != len(methodologies):
            self.save_methodologies(new_methodologies)
            return True
        return False

    def delete_methodologies_batch(self, method_ids: list[str]):
        ids = {str(i).strip() for i in (method_ids or []) if str(i).strip()}
        if not ids:
            return {"success": False, "deleted_count": 0, "remaining_count": len(self.load_methodologies())}
        methodologies = self.load_methodologies()
        new_methodologies = [m for m in methodologies if str(m.get("method_id", "")) not in ids]
        deleted_count = len(methodologies) - len(new_methodologies)
        if deleted_count > 0:
            self.save_methodologies(new_methodologies)
        return {
            "success": deleted_count > 0,
            "deleted_count": deleted_count,
            "remaining_count": len(new_methodologies),
        }

    def update_methodology_category(self, method_id: str, category: str):
        methodologies = self.load_methodologies()
        for i, method in enumerate(methodologies):
            if method["method_id"] == method_id:
                normalized = self.normalize_methodology(method)
                normalized["category"] = (category or "").strip() or normalized.get("category", "通用/其他")
                normalized["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                methodologies[i] = normalized
                self.save_methodologies(methodologies)

                feedback = self._load_category_feedback()
                event_key = normalized.get("event_key", "")
                if event_key:
                    feedback[event_key] = normalized["category"]
                    self._save_category_feedback(feedback)
                return normalized
        return None

    # 根据ID获取方法论
    def get_methodology_by_id(self, method_id):
        methodologies = self.load_methodologies()
        for method in methodologies:
            if method["method_id"] == method_id:
                return method
        return None
