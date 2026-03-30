import json
import os
import time
import uuid
from typing import List, Dict, Any

class ShortTermMemory:
    def __init__(self):
        self.task_id = ""
        self.user_input = ""
        self.temporal_risk = "low"  # high：强时效事实（天气/股价等），结论不可照抄旧答案
        self.current_step = 0
        self.sub_tasks = []
        self.agent_status = {}  # agent_id: status
        self.results = []
        self.logs = []
    
    def clear(self):
        """任务结束后清空短期记忆"""
        self.task_id = ""
        self.user_input = ""
        self.temporal_risk = "low"
        self.current_step = 0
        self.sub_tasks = []
        self.agent_status = {}
        self.results = []
        self.logs = []

class MidTermMemory:
    def __init__(self):
        self.task_templates = []  # 如：市场分析、报告生成
        self.agent_combinations = []  # 常用Agent组合
        self.last_task_flow = []
        self.common_prompts = []
    
    def load(self):
        """加载中期记忆"""
        try:
            with open("data/midterm_memory.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                self.task_templates = data.get("task_templates", [])
                self.agent_combinations = data.get("agent_combinations", [])
                self.last_task_flow = data.get("last_task_flow", [])
                self.common_prompts = data.get("common_prompts", [])
        except:
            pass
    
    def save(self):
        """保存中期记忆"""
        data = {
            "task_templates": self.task_templates,
            "agent_combinations": self.agent_combinations,
            "last_task_flow": self.last_task_flow,
            "common_prompts": self.common_prompts
        }
        with open("data/midterm_memory.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

class LongTermMemory:
    def __init__(self):
        self.methodologies = []  # 解决方案
        self.best_cases = []     # 成功案例
        self.knowledge_base = [] # 行业/领域知识
        self.ab_stats = {
            "pairs": {},
            "methods": {},
            "updated_at": "",
        }

    def _split_keywords(self, keywords):
        if keywords is None:
            return []
        if isinstance(keywords, list):
            return [str(k).strip() for k in keywords if str(k).strip()]
        if isinstance(keywords, str):
            import re
            parts = re.split(r"[\s,，;；/|]+", keywords.strip())
            return [p.strip() for p in parts if p.strip()]
        return [str(keywords).strip()] if str(keywords).strip() else []

    def _normalize_methodology(self, methodology: dict) -> dict:
        """兼容不同版本字段：scene/scenario、keywords/core_keywords。"""
        if not isinstance(methodology, dict):
            return {}
        m = dict(methodology)

        scene = m.get("scene")
        scenario = m.get("scenario")
        if not scenario:
            scenario = scene or ""
        if not scene:
            scene = scenario

        keywords = m.get("keywords")
        core_keywords = m.get("core_keywords")
        if keywords is None and core_keywords is not None:
            keywords = core_keywords
        if core_keywords is None and keywords is not None:
            core_keywords = keywords

        keywords_list = self._split_keywords(keywords)
        core_keywords_list = self._split_keywords(core_keywords)

        solve_steps = m.get("solve_steps", [])
        if isinstance(solve_steps, str):
            import re
            solve_steps = [s.strip() for s in re.split(r"[\n;；]+", solve_steps) if s.strip()]

        m["scene"] = scene
        m["scenario"] = scenario
        m["keywords"] = keywords_list
        m["core_keywords"] = core_keywords_list or keywords_list
        m["solve_steps"] = solve_steps if isinstance(solve_steps, list) else []
        # 同事件归并键：场景标准化 + 前4个关键词
        scene_norm = str(scene or "").strip().lower()
        kw_norm = sorted({str(k).strip().lower() for k in (m["keywords"] or []) if str(k).strip()})
        m["event_key"] = m.get("event_key") or f"{scene_norm}|{'/'.join(kw_norm[:4])}"
        m.setdefault("method_id", str(uuid.uuid4()))
        m.setdefault("success_count", 0)
        m.setdefault("usage_count", 0)
        m.setdefault("create_time", time.strftime("%Y-%m-%d %H:%M:%S"))
        m.setdefault("updated_at", m.get("create_time"))
        try:
            ver = int(m.get("version", 1))
        except Exception:
            ver = 1
        m["version"] = max(1, ver)
        m.setdefault("parent_version", None if m["version"] <= 1 else (m["version"] - 1))
        try:
            m["score"] = float(m.get("score", 0.0))
        except Exception:
            m["score"] = 0.0
        refs = m.get("evidence_refs", [])
        m["evidence_refs"] = refs if isinstance(refs, list) else []
        metrics = m.get("quality_metrics", {})
        m["quality_metrics"] = metrics if isinstance(metrics, dict) else {}
        rh = m.get("version_history", [])
        m["version_history"] = rh if isinstance(rh, list) else []
        return m

    def _methodology_text(self, method: dict) -> str:
        """将方法论的关键字段拼接成可向量化文本。"""
        if not isinstance(method, dict):
            return ""
        scene = method.get("scene") or method.get("scenario") or ""
        keywords = method.get("keywords") or method.get("core_keywords") or []
        solve_steps = method.get("solve_steps") or []
        applicable_range = method.get("applicable_range", "") or ""

        if isinstance(keywords, list):
            kw_text = " ".join([str(k) for k in keywords if str(k).strip()])
        else:
            kw_text = str(keywords)

        if isinstance(solve_steps, list):
            steps_text = " ".join([str(s) for s in solve_steps if str(s).strip()])
        else:
            steps_text = str(solve_steps)

        return " ".join([str(scene), str(kw_text), str(steps_text), str(applicable_range)]).strip()

    def _safe_float(self, v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _safe_int(self, v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    def _quality_factor(self, method: dict) -> float:
        """
        检索质量加权：
        - score 越低越降权（解决“低分版本继续被频繁命中”）
        - success_ratio 越低越降权
        - usage 很多但成功率差时进一步降权
        """
        score = self._safe_float(method.get("score", 0.0), 0.0)
        score = max(0.0, min(1.0, score))
        usage = max(0, self._safe_int(method.get("usage_count", 0), 0))
        success = max(0, self._safe_int(method.get("success_count", 0), 0))
        success_ratio = (success / usage) if usage > 0 else (1.0 if success > 0 else 0.5)

        factor = (0.55 + 0.45 * score) * (0.70 + 0.30 * success_ratio)
        # 低分版本显式降权
        if score < 0.35:
            factor *= 0.60
        elif score < 0.50:
            factor *= 0.82
        # 被频繁使用却低成功率，继续降权
        if usage >= 3 and success_ratio < 0.34:
            factor *= 0.65
        return max(0.15, min(1.25, factor))

    def _tokenize_for_vector(self, text: str) -> list[str]:
        """
        轻量 embedding：不用外部依赖，基于
        1) 英文/数字词
        2) 中文字符的 2/3-gram
        """
        if text is None:
            return []
        s = str(text).strip().lower()
        if not s:
            return []

        import re

        words = re.findall(r"[a-z0-9_]+", s)
        cjk_chars = [ch for ch in s if "\u4e00" <= ch <= "\u9fff"]
        ngrams: list[str] = []
        for n in (2, 3):
            for i in range(0, max(0, len(cjk_chars) - n + 1)):
                gram = "".join(cjk_chars[i : i + n])
                if gram:
                    ngrams.append(gram)

        # 去重但保持顺序，避免 token 数量爆炸
        return list(dict.fromkeys(words + ngrams))

    def _build_tfidf_vectors(self, corpus_texts: list[str]) -> tuple[dict[str, float], list[dict[str, float]]]:
        """为语料构建 IDF，并为每条语料生成归一化 TF-IDF 稀疏向量。"""
        from math import log, sqrt
        from collections import Counter

        tokenized = [self._tokenize_for_vector(t) for t in corpus_texts]
        tokenized = [toks for toks in tokenized if toks]
        if not tokenized:
            return {}, []

        df: dict[str, int] = {}
        for toks in tokenized:
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1

        n_docs = len(tokenized)
        idf: dict[str, float] = {tok: log((n_docs + 1) / (dfi + 1)) + 1.0 for tok, dfi in df.items()}

        vectors: list[dict[str, float]] = []
        for toks in tokenized:
            counts = Counter(toks)
            total = max(1, sum(counts.values()))
            vec: dict[str, float] = {}
            for tok, cnt in counts.items():
                if tok not in idf:
                    continue
                tf = cnt / total
                vec[tok] = tf * idf[tok]

            # 归一化到单位向量，便于余弦相似度
            norm = sqrt(sum(w * w for w in vec.values())) or 1.0
            for tok in list(vec.keys()):
                vec[tok] = vec[tok] / norm
            vectors.append(vec)

        return idf, vectors

    def _cosine_similarity_from_vectors(self, a: dict[str, float], b: dict[str, float]) -> float:
        """计算两个稀疏向量余弦相似度（假设已归一化）。"""
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        dot = 0.0
        for tok, wa in a.items():
            dot += wa * b.get(tok, 0.0)
        return float(dot)

    def _vectorize_query(self, query_text: str, idf: dict[str, float]) -> dict[str, float]:
        """用已计算好的 IDF 对查询做 TF-IDF 稀疏向量化并归一化。"""
        from math import sqrt
        from collections import Counter

        toks = self._tokenize_for_vector(query_text)
        if not toks or not idf:
            return {}

        counts = Counter(toks)
        total = max(1, sum(counts.values()))
        vec: dict[str, float] = {}
        for tok, cnt in counts.items():
            if tok not in idf:
                continue
            tf = cnt / total
            vec[tok] = tf * idf[tok]

        norm = sqrt(sum(w * w for w in vec.values())) or 1.0
        for tok in list(vec.keys()):
            vec[tok] = vec[tok] / norm
        return vec
    
    def load(self):
        """加载长期记忆"""
        try:
            with open("data/methodology/methodologies.json", "r", encoding="utf-8") as f:
                self.methodologies = json.load(f)
        except:
            self.methodologies = []
        self._load_ab_stats()
    
    def save(self):
        """保存长期记忆"""
        os.makedirs("data/methodology", exist_ok=True)
        with open("data/methodology/methodologies.json", "w", encoding="utf-8") as f:
            json.dump(self.methodologies, f, ensure_ascii=False, indent=2)
        self._save_ab_stats()

    def _ab_stats_path(self) -> str:
        return "data/methodology/ab_stats.json"

    def _load_ab_stats(self):
        try:
            with open(self._ab_stats_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                pairs = data.get("pairs") if isinstance(data.get("pairs"), dict) else {}
                methods = data.get("methods") if isinstance(data.get("methods"), dict) else {}
                self.ab_stats = {
                    "pairs": pairs,
                    "methods": methods,
                    "updated_at": str(data.get("updated_at") or ""),
                }
                return
        except Exception:
            pass
        self.ab_stats = {"pairs": {}, "methods": {}, "updated_at": ""}

    def _save_ab_stats(self):
        os.makedirs("data/methodology", exist_ok=True)
        self.ab_stats["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._ab_stats_path(), "w", encoding="utf-8") as f:
            json.dump(self.ab_stats, f, ensure_ascii=False, indent=2)

    def _pair_key(self, method_a: str, method_b: str) -> str:
        a = str(method_a or "").strip()
        b = str(method_b or "").strip()
        if not a or not b:
            return ""
        left, right = sorted([a, b])
        return f"{left}|{right}"

    def record_method_hit(self, method_id: str, retrieval_score: float = 0.0):
        mid = str(method_id or "").strip()
        if not mid:
            return
        methods = self.ab_stats.setdefault("methods", {})
        item = methods.get(mid) if isinstance(methods.get(mid), dict) else {}
        item["hits"] = int(item.get("hits", 0) or 0) + 1
        item["retrieval_score_avg"] = round(
            (
                float(item.get("retrieval_score_avg", 0.0) or 0.0) * max(0, int(item["hits"]) - 1)
                + float(retrieval_score or 0.0)
            )
            / max(1, int(item["hits"])),
            4,
        )
        item["last_hit_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        methods[mid] = item
        self._save_ab_stats()

    def record_method_outcome(self, method_id: str, is_success: bool, quality_score: float = 0.0):
        mid = str(method_id or "").strip()
        if not mid:
            return
        methods = self.ab_stats.setdefault("methods", {})
        item = methods.get(mid) if isinstance(methods.get(mid), dict) else {}
        outcomes = int(item.get("outcomes", 0) or 0) + 1
        succ = int(item.get("successes", 0) or 0) + (1 if bool(is_success) else 0)
        item["outcomes"] = outcomes
        item["successes"] = succ
        item["success_rate"] = round(succ / max(1, outcomes), 4)
        prev_avg = float(item.get("quality_score_avg", 0.0) or 0.0)
        item["quality_score_avg"] = round((prev_avg * (outcomes - 1) + float(quality_score or 0.0)) / outcomes, 4)
        item["last_quality_score"] = round(float(quality_score or 0.0), 4)
        item["last_outcome_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        methods[mid] = item
        self._save_ab_stats()

    def record_ab_outcome(self, ab_meta: dict, is_success: bool, quality_score: float = 0.0):
        if not isinstance(ab_meta, dict):
            return
        if str(ab_meta.get("mode") or "") != "ab_bandit":
            return
        arms = ab_meta.get("arms") if isinstance(ab_meta.get("arms"), dict) else {}
        a_id = str((arms.get("A") or {}).get("method_id") or "").strip()
        b_id = str((arms.get("B") or {}).get("method_id") or "").strip()
        chosen = str(ab_meta.get("chosen") or "").strip().upper()
        pair_key = self._pair_key(a_id, b_id)
        if not pair_key:
            return
        pairs = self.ab_stats.setdefault("pairs", {})
        pair = pairs.get(pair_key) if isinstance(pairs.get(pair_key), dict) else {}
        for arm_name, mid in (("A", a_id), ("B", b_id)):
            if arm_name not in pair or not isinstance(pair.get(arm_name), dict):
                pair[arm_name] = {"method_id": mid, "trials": 0, "wins": 0}
            else:
                pair[arm_name]["method_id"] = mid
        pair["trials"] = int(pair.get("trials", 0) or 0) + 1
        if chosen in ("A", "B"):
            picked = pair[chosen]
            picked["trials"] = int(picked.get("trials", 0) or 0) + 1
            if bool(is_success):
                picked["wins"] = int(picked.get("wins", 0) or 0) + 1
            pair[chosen] = picked
        pair["last_outcome"] = {
            "chosen": chosen,
            "is_success": bool(is_success),
            "quality_score": round(float(quality_score or 0.0), 4),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        pairs[pair_key] = pair
        self._save_ab_stats()

    def get_adaptive_ab_epsilon(self, default_epsilon: float = 0.2) -> float:
        """
        按整体对战胜率动态调探索：
        - 模型库稳定（平均胜率高）=> 降低探索
        - 模型库不稳定 => 提高探索
        """
        base = max(0.0, min(0.5, float(default_epsilon)))
        pairs = self.ab_stats.get("pairs") if isinstance(self.ab_stats.get("pairs"), dict) else {}
        if not pairs:
            return base
        win_rates: list[float] = []
        for _, pair in pairs.items():
            if not isinstance(pair, dict):
                continue
            for arm in ("A", "B"):
                arm_data = pair.get(arm) if isinstance(pair.get(arm), dict) else {}
                trials = int(arm_data.get("trials", 0) or 0)
                wins = int(arm_data.get("wins", 0) or 0)
                if trials >= 3:
                    win_rates.append(wins / max(1, trials))
        if not win_rates:
            return base
        avg = sum(win_rates) / max(1, len(win_rates))
        if avg >= 0.72:
            return round(max(0.05, base - 0.08), 4)
        if avg <= 0.45:
            return round(min(0.45, base + 0.08), 4)
        return round(base, 4)

    def methodology_health_dashboard(self, limit: int = 100) -> list[dict]:
        methods = self.get_all_methodologies()
        stats_map = self.ab_stats.get("methods") if isinstance(self.ab_stats.get("methods"), dict) else {}
        rows: list[dict] = []
        for m in methods:
            mid = str(m.get("method_id") or "")
            st = stats_map.get(mid) if isinstance(stats_map.get(mid), dict) else {}
            rows.append(
                {
                    "method_id": mid,
                    "title": m.get("title") or m.get("scene") or "",
                    "category": m.get("category") or "通用/其他",
                    "version": int(m.get("version", 1) or 1),
                    "score": round(float(m.get("score", 0.0) or 0.0), 4),
                    "usage_count": int(m.get("usage_count", 0) or 0),
                    "success_count": int(m.get("success_count", 0) or 0),
                    "hits": int(st.get("hits", 0) or 0),
                    "outcomes": int(st.get("outcomes", 0) or 0),
                    "success_rate": round(float(st.get("success_rate", 0.0) or 0.0), 4),
                    "quality_score_avg": round(float(st.get("quality_score_avg", 0.0) or 0.0), 4),
                    "last_outcome_at": st.get("last_outcome_at", ""),
                    "updated_at": m.get("updated_at") or m.get("create_time") or "",
                }
            )
        rows.sort(
            key=lambda x: (
                float(x.get("quality_score_avg", 0.0) or 0.0),
                float(x.get("score", 0.0) or 0.0),
                int(x.get("hits", 0) or 0),
            ),
            reverse=True,
        )
        lim = max(1, min(500, int(limit or 100)))
        return rows[:lim]
    
    def add_methodology(self, methodology, keywords=None, solve_steps=None):
        """添加方法论，包含相似度检测和去重机制"""
        # 兼容旧调用：add_methodology(scene, keywords, solve_steps)
        if not isinstance(methodology, dict):
            methodology = {
                "scene": str(methodology or ""),
                "keywords": keywords or [],
                "solve_steps": solve_steps or [],
            }
        methodology = self._normalize_methodology(methodology)
        if not methodology:
            return
        # 检查是否存在相似方法论
        similar_method = self.find_similar_methodology(methodology)
        if similar_method:
            # 相似度≥70%，更新已有方法论
            self.update_methodology(similar_method, methodology)
        else:
            # 不存在相似方法论，添加新的
            self.methodologies.append(methodology)
            self.save()
        return methodology
    
    def find_similar_methodology(self, new_methodology):
        """查找相似方法论，相似度≥70%返回对应方法论"""
        new_methodology = self._normalize_methodology(new_methodology)
        new_text = self._methodology_text(new_methodology)
        if not new_text:
            return None

        # 先走强规则：event_key完全一致直接判定为同事件
        new_key = new_methodology.get("event_key", "")
        if new_key:
            for method in self.methodologies:
                method_n = self._normalize_methodology(method)
                if method_n.get("event_key", "") == new_key:
                    return method_n

        if not self.methodologies:
            return None

        corpus_texts: list[str] = []
        normalized_methods: list[dict] = []
        for method in self.methodologies:
            method_n = self._normalize_methodology(method)
            normalized_methods.append(method_n)
            corpus_texts.append(self._methodology_text(method_n))

        idf, vectors = self._build_tfidf_vectors(corpus_texts)
        if not idf or not vectors:
            return None

        query_vec = self._vectorize_query(new_text, idf)
        if not query_vec:
            return None

        best_sim = 0.0
        best_method = None
        new_scenario = new_methodology.get("scenario") or ""
        for method_n, vec in zip(normalized_methods, vectors):
            cos_sim = self._cosine_similarity_from_vectors(query_vec, vec)
            # 场景完全一致时，提升相似度以更符合“同场景相似=同方案”的业务直觉
            scenario_exact = 1.0 if (new_scenario and (method_n.get("scenario") == new_scenario)) else 0.0
            sim = cos_sim * 0.85 + scenario_exact * 0.15
            if sim > best_sim:
                best_sim = sim
                best_method = method_n

        # README：相似度>=70%认为相似并去重/更新
        if best_sim >= 0.7:
            return best_method
        return None
    
    def _append_version_snapshot(self, method: dict):
        history = method.get("version_history") if isinstance(method.get("version_history"), list) else []
        snapshot = {
            "version": int(method.get("version", 1) or 1),
            "updated_at": str(method.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
            "score": float(method.get("score", 0.0) or 0.0),
            "solve_steps": list(method.get("solve_steps") or []),
            "keywords": list(method.get("keywords") or []),
            "scene": str(method.get("scene") or ""),
            "quality_metrics": dict(method.get("quality_metrics") or {}),
        }
        history.append(snapshot)
        # 控制历史体积
        method["version_history"] = history[-20:]

    def update_methodology(self, old_method, new_method_data):
        """更新已有方法论"""
        new_method_data = self._normalize_methodology(new_method_data)
        self._append_version_snapshot(old_method)
        prev_ver = int(old_method.get("version", 1) or 1)
        # 更新字段
        for key, value in new_method_data.items():
            if key not in ["method_id", "create_time", "success_count", "usage_count", "is_success", "version_history"]:
                old_method[key] = value
        
        # 更新使用次数和时间
        old_method["usage_count"] = old_method.get("usage_count", 0) + 1
        old_method["last_update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        old_method["updated_at"] = old_method["last_update_time"]
        old_method["parent_version"] = prev_ver
        old_method["version"] = max(prev_ver + 1, int(new_method_data.get("version", prev_ver + 1) or (prev_ver + 1)))

        # 更新成功次数（由上层传入 is_success 控制）
        if bool(new_method_data.get("is_success", False)):
            old_method["success_count"] = old_method.get("success_count", 0) + 1
        
        self.save()
        return old_method
    
    def search_methodology(self, query):
        """搜索方法论（简单实现）"""
        if isinstance(query, list):
            query = " ".join([str(q) for q in query])
        query = query or ""
        if not query:
            return []

        if not self.methodologies:
            return []

        corpus_texts: list[str] = []
        normalized_methods: list[dict] = []
        for method in self.methodologies:
            method_n = self._normalize_methodology(method)
            normalized_methods.append(method_n)
            corpus_texts.append(self._methodology_text(method_n))

        idf, vectors = self._build_tfidf_vectors(corpus_texts)
        if not idf or not vectors:
            return []

        query_vec = self._vectorize_query(str(query), idf)
        if not query_vec:
            return []

        results: list[tuple[float, dict]] = []
        for method_n, vec in zip(normalized_methods, vectors):
            raw_sim = self._cosine_similarity_from_vectors(query_vec, vec)
            if raw_sim <= 0:
                continue
            q_factor = self._quality_factor(method_n)
            adjusted_sim = raw_sim * q_factor
            method_with_meta = dict(method_n)
            method_with_meta["retrieval_meta"] = {
                "raw_similarity": round(raw_sim, 4),
                "quality_factor": round(q_factor, 4),
                "adjusted_similarity": round(adjusted_sim, 4),
            }
            results.append((adjusted_sim, method_with_meta))

        results.sort(key=lambda x: x[0], reverse=True)
        return results

    # --- 兼容 MethodologyLibrary 的接口，统一为单一事实源 ---
    def search_methodologies(self, query):
        return [m for _, m in self.search_methodology(query)]

    def get_all_methodologies(self):
        return [self._normalize_methodology(m) for m in (self.methodologies or []) if isinstance(m, dict)]

    def delete_methodology(self, method_id):
        mid = str(method_id or "").strip()
        if not mid:
            return False
        before = len(self.methodologies)
        self.methodologies = [m for m in self.methodologies if str((m or {}).get("method_id", "")) != mid]
        changed = len(self.methodologies) != before
        if changed:
            self.save()
        return changed

    def delete_methodologies_batch(self, method_ids: list[str]):
        ids = {str(i).strip() for i in (method_ids or []) if str(i).strip()}
        if not ids:
            return {"success": False, "deleted_count": 0, "remaining_count": len(self.methodologies)}
        before = len(self.methodologies)
        self.methodologies = [m for m in self.methodologies if str((m or {}).get("method_id", "")) not in ids]
        deleted = before - len(self.methodologies)
        if deleted > 0:
            self.save()
        return {"success": deleted > 0, "deleted_count": deleted, "remaining_count": len(self.methodologies)}

    def update_methodology_category(self, method_id: str, category: str):
        mid = str(method_id or "").strip()
        if not mid:
            return None
        for idx, method in enumerate(self.methodologies):
            m = self._normalize_methodology(method)
            if str(m.get("method_id", "")) != mid:
                continue
            self._append_version_snapshot(m)
            prev_ver = int(m.get("version", 1) or 1)
            m["category"] = (category or "").strip() or m.get("category", "通用/其他")
            m["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            m["parent_version"] = prev_ver
            m["version"] = prev_ver + 1
            self.methodologies[idx] = m
            self.save()
            return m
        return None

    def get_methodology_by_id(self, method_id):
        mid = str(method_id or "").strip()
        if not mid:
            return None
        for method in self.methodologies:
            m = self._normalize_methodology(method)
            if str(m.get("method_id", "")) == mid:
                return m
        return None

    def rollback_methodology(self, method_id: str, to_version: int):
        mid = str(method_id or "").strip()
        if not mid:
            return None
        target = int(to_version or 0)
        if target <= 0:
            return None
        for idx, method in enumerate(self.methodologies):
            m = self._normalize_methodology(method)
            if str(m.get("method_id", "")) != mid:
                continue
            history = m.get("version_history", [])
            snap = next((h for h in history if int(h.get("version", 0) or 0) == target), None)
            if not isinstance(snap, dict):
                return None
            self._append_version_snapshot(m)
            prev_ver = int(m.get("version", 1) or 1)
            m["solve_steps"] = list(snap.get("solve_steps") or m.get("solve_steps") or [])
            m["keywords"] = list(snap.get("keywords") or m.get("keywords") or [])
            m["scene"] = str(snap.get("scene") or m.get("scene") or "")
            m["score"] = float(snap.get("score", m.get("score", 0.0)) or 0.0)
            m["quality_metrics"] = dict(snap.get("quality_metrics") or m.get("quality_metrics") or {})
            m["rollback_to"] = target
            m["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            m["parent_version"] = prev_ver
            m["version"] = prev_ver + 1
            self.methodologies[idx] = m
            self.save()
            return m
        return None
