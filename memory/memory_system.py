import json
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
        m.setdefault("success_count", 0)
        m.setdefault("usage_count", 0)
        m.setdefault("create_time", time.strftime("%Y-%m-%d %H:%M:%S"))
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
    
    def save(self):
        """保存长期记忆"""
        with open("data/methodology/methodologies.json", "w", encoding="utf-8") as f:
            json.dump(self.methodologies, f, ensure_ascii=False, indent=2)
    
    def add_methodology(self, methodology):
        """添加方法论，包含相似度检测和去重机制"""
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
    
    def update_methodology(self, old_method, new_method_data):
        """更新已有方法论"""
        new_method_data = self._normalize_methodology(new_method_data)
        # 更新字段
        for key, value in new_method_data.items():
            if key not in ["method_id", "create_time", "success_count", "usage_count", "is_success"]:
                old_method[key] = value
        
        # 更新使用次数和时间
        old_method["usage_count"] = old_method.get("usage_count", 0) + 1
        old_method["last_update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # 更新成功次数（由上层传入 is_success 控制）
        if bool(new_method_data.get("is_success", False)):
            old_method["success_count"] = old_method.get("success_count", 0) + 1
        
        self.save()
    
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
            sim = self._cosine_similarity_from_vectors(query_vec, vec)
            if sim > 0:
                results.append((sim, method_n))

        results.sort(key=lambda x: x[0], reverse=True)
        return results
