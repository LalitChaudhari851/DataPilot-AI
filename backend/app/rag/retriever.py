"""
Hybrid Retriever — Combines vector search (ChromaDB) with keyword search (BM25).
Uses Reciprocal Rank Fusion (RRF) to merge results from both retrieval methods.
Supports database-aware retrieval with strict database isolation.
"""

import os
import re
import time
from typing import Optional, Any

import chromadb
import structlog
from rank_bm25 import BM25Okapi

from app.rag.schema_enricher import SchemaEnricher

logger = structlog.get_logger()


class NoopCollection:
    """Minimal Chroma collection stand-in for local startup without vector search."""

    def count(self) -> int:
        return 0

    def get(self) -> dict:
        return {"ids": []}

    def delete(self, ids: list[str]):
        return None

    def add(self, documents: list[str], metadatas: list[dict], ids: list[str]):
        return None

    def query(self, query_texts: list[str], n_results: int, where: Optional[dict] = None) -> dict:
        return {"documents": [[]]}


def detect_exact_table_mentions(query: str, available_tables: list[str]) -> list[str]:
    """
    Detect tables in available_tables that are explicitly mentioned in the natural language query.
    
    Uses token/word boundary matching and subspan suppression so:
    - 'player' matches table 'player'
    - 'player' does NOT match 'player_award' or 'player_college'
    - Compound names like 'player_award' or 'player award' are correctly detected
    - Supports plural/singular inflection (e.g. 'customers' <-> 'customer')
    - Case-insensitive
    """
    if not query or not available_tables:
        return []

    query_lower = query.lower()
    matches = []
    matched_spans = []  # list of (start, end)

    # Sort available tables by length descending so longer/more specific names match first
    sorted_tables = sorted(available_tables, key=len, reverse=True)

    def is_subspan(span):
        return any(s[0] <= span[0] and span[1] <= s[1] for s in matched_spans)

    for table in sorted_tables:
        t_low = table.lower()
        if len(t_low) < 2:
            continue

        # 1. Exact match for table name as a whole word/token
        pattern_exact = rf"\b{re.escape(t_low)}\b"
        found = False
        for m in re.finditer(pattern_exact, query_lower):
            if not is_subspan(m.span()):
                matches.append(table)
                matched_spans.append(m.span())
                found = True
                break
        if found:
            continue

        # 2. If table contains underscores, check space-separated equivalent
        if "_" in t_low:
            t_spaced = t_low.replace("_", " ")
            for m in re.finditer(rf"\b{re.escape(t_spaced)}\b", query_lower):
                if not is_subspan(m.span()):
                    matches.append(table)
                    matched_spans.append(m.span())
                    found = True
                    break
            if found:
                continue

        # 3. Simple inflection: plural <-> singular (for tables with length >= 4)
        if len(t_low) >= 4:
            if t_low.endswith("s") and not t_low.endswith("ss"):
                sing = t_low[:-1]
                for m in re.finditer(rf"\b{re.escape(sing)}\b", query_lower):
                    if not is_subspan(m.span()):
                        matches.append(table)
                        matched_spans.append(m.span())
                        found = True
                        break
            else:
                plur = t_low + "s"
                for m in re.finditer(rf"\b{re.escape(plur)}\b", query_lower):
                    if not is_subspan(m.span()):
                        matches.append(table)
                        matched_spans.append(m.span())
                        found = True
                        break

    return matches


class HybridRetriever:
    """
    Production Database-Aware Hybrid RAG retriever combining:
    1. ChromaDB vector similarity (semantic search per db_id collection)
    2. BM25 keyword matching (exact table/column name matching per db_id index)
    3. Reciprocal Rank Fusion (RRF) to merge results
    4. Optional cross-encoder reranking for precision

    Database Isolation:
    - ChromaDB: separate collection per db_id + mandatory where={"db_id": db_id} metadata filtering
    - BM25: separate BM25Okapi index per db_id
    """

    def __init__(self, db_pool, chroma_persist_dir: str = "./chroma_db", registry=None):
        self.db_pool = db_pool
        self.chroma_persist_dir = chroma_persist_dir
        self.vector_enabled = os.getenv("DISABLE_VECTOR_RAG", "").lower() not in {"1", "true", "yes"}

        from app.db.registry import get_database_registry
        self.registry = registry or get_database_registry()

        # Multi-database indexes
        self._collections: dict[str, Any] = {}
        self._documents: dict[str, list[str]] = {}
        self._metadatas: dict[str, list[dict]] = {}
        self._doc_ids: dict[str, list[str]] = {}
        self._bm25_indices: dict[str, BM25Okapi] = {}

        # Business Knowledge RAG indexes per db_id
        self._business_defs: dict[str, list[Any]] = {}
        self._business_documents: dict[str, list[str]] = {}
        self._business_metadatas: dict[str, list[dict]] = {}
        self._business_bm25_indices: dict[str, BM25Okapi] = {}

        # Embedding function
        self.embedding_function = None
        if self.vector_enabled:
            self.chroma_client = chromadb.PersistentClient(path=chroma_persist_dir)
            hf_token = os.getenv("HUGGINGFACEHUB_API_TOKEN")
            if hf_token:
                try:
                    from chromadb.utils.embedding_functions import HuggingFaceEmbeddingFunction
                    self.embedding_function = HuggingFaceEmbeddingFunction(
                        api_key=hf_token,
                        model_name="sentence-transformers/all-MiniLM-L6-v2"
                    )
                    logger.info("using_huggingface_embedding_function")
                except Exception as e:
                    logger.warning("hf_embedding_init_failed", error=str(e))
        else:
            self.chroma_client = None
            logger.warning("vector_rag_disabled")

        # Optional cross-encoder reranker — lazy-loaded on first use
        self._reranker = None
        self._reranker_loaded = False

        # Index default database on startup
        self._index_schema_safe(chroma_persist_dir)

    # ── Backward Compatibility Properties ──────────────────────────

    @property
    def collection(self):
        """Default collection for backward compatibility."""
        return self._get_or_create_collection("default")

    @property
    def documents(self) -> list[str]:
        """Default documents list for backward compatibility."""
        return self._documents.get("default", [])

    @documents.setter
    def documents(self, val: list[str]):
        self._documents["default"] = val

    @property
    def doc_ids(self) -> list[str]:
        """Default doc_ids list for backward compatibility."""
        return self._doc_ids.get("default", [])

    @doc_ids.setter
    def doc_ids(self, val: list[str]):
        self._doc_ids["default"] = val

    @property
    def bm25(self) -> Optional[BM25Okapi]:
        """Default BM25 index for backward compatibility."""
        return self._bm25_indices.get("default")

    @bm25.setter
    def bm25(self, val: Optional[BM25Okapi]):
        if val is not None:
            self._bm25_indices["default"] = val

    # ── Chroma Collection Management ───────────────────────────────

    def _get_collection_name(self, db_id: str) -> str:
        """
        Generate a valid ChromaDB collection name for a database ID.
        Must be 3-63 characters, start and end with an alphanumeric character.
        """
        if db_id == "default":
            return "schema_knowledge_v2"
        safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in db_id)
        if not safe.startswith("schema_"):
            safe = f"schema_{safe}"
        return safe[:63]

    def _get_or_create_collection(self, db_id: str):
        """Get or lazily create the ChromaDB collection for a given database ID."""
        if not self.vector_enabled or self.chroma_client is None:
            return NoopCollection()

        if db_id not in self._collections:
            col_name = self._get_collection_name(db_id)
            self._collections[db_id] = self.chroma_client.get_or_create_collection(
                name=col_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=self.embedding_function,
            )
        return self._collections[db_id]

    # ── Indexing Methods ───────────────────────────────────────────

    def _index_schema_safe(self, chroma_persist_dir: str):
        """Index default schema with a file lock to prevent concurrent writes."""
        lock_path = os.path.join(chroma_persist_dir, ".index_lock")
        try:
            import filelock
            lock = filelock.FileLock(lock_path, timeout=30)
            with lock:
                self._index_schema()
        except ImportError:
            self._index_schema()
        except Exception as e:
            logger.warning("index_lock_warning", error=str(e))
            self._index_schema()

    def _index_schema(self):
        """Index default database on startup."""
        self.index_database("default", db_pool=self.db_pool, force=False)

    def index_database(self, db_id: str = "default", db_pool=None, force: bool = False) -> int:
        """
        Index all tables of a database into both ChromaDB and BM25.
        
        Args:
            db_id: Database identifier (e.g. 'E_commerce', 'default')
            db_pool: Optional DatabasePool / SQLitePool instance (resolves from registry if None)
            force: If True, re-indexes even if already indexed
            
        Returns:
            Number of tables indexed
        """
        if not force and db_id in self._bm25_indices and len(self._documents.get(db_id, [])) > 0:
            return len(self._documents[db_id])

        # Resolve pool
        pool = db_pool
        if pool is None:
            if db_id == "default":
                pool = self.db_pool
            else:
                from app.db.registry import resolve_database
                try:
                    _, _, pool = resolve_database(db_id, default_pool=self.db_pool, registry=self.registry)
                except Exception as e:
                    logger.warning("index_database_pool_resolution_failed", db_id=db_id, error=str(e))
                    return 0

        if pool is None:
            logger.warning("index_database_failed_no_pool", db_id=db_id)
            return 0

        enricher = SchemaEnricher(pool)
        enriched_tables = enricher.enrich_all_tables(db_id=db_id)

        if not enriched_tables:
            logger.warning("no_tables_to_index", db_id=db_id)
            return 0

        documents = []
        metadatas = []
        ids = []

        for item in enriched_tables:
            documents.append(item["document"])
            meta = dict(item["metadata"])
            meta["db_id"] = db_id
            meta["table_name"] = item["table_name"]
            meta["document_type"] = "table_schema"
            metadatas.append(meta)
            ids.append(f"{db_id}_{item['table_name']}")

        # Index in ChromaDB collection for this db_id
        if self.vector_enabled and self.chroma_client:
            collection = self._get_or_create_collection(db_id)
            current_count = collection.count()
            if force or current_count != len(enriched_tables):
                if current_count > 0:
                    try:
                        existing = collection.get()
                        if existing.get("ids"):
                            schema_ids = [i for i in existing["ids"] if not i.startswith("bdef_")]
                            if schema_ids:
                                collection.delete(ids=schema_ids)
                    except Exception as e:
                        logger.warning("chroma_clear_failed", db_id=db_id, error=str(e))

                try:
                    collection.add(
                        documents=documents,
                        metadatas=metadatas,
                        ids=ids,
                    )
                except Exception as e:
                    logger.error("chroma_add_failed", db_id=db_id, error=str(e))

        # Build BM25 index for this db_id
        self._documents[db_id] = documents
        self._metadatas[db_id] = metadatas
        self._doc_ids[db_id] = ids
        tokenized = [doc.lower().split() for doc in documents]
        self._bm25_indices[db_id] = BM25Okapi(tokenized)

        logger.info(
            "database_indexed",
            db_id=db_id,
            tables=len(enriched_tables),
            collection_name=self._get_collection_name(db_id),
            has_bm25=True,
        )

        return len(enriched_tables)

    # ── Retrieval Methods ──────────────────────────────────────────

    def detect_exact_table_mentions(self, query: str, db_id: str = "default") -> list[str]:
        """Detect tables belonging to db_id that are explicitly referenced in query."""
        if db_id not in self._metadatas:
            self.index_database(db_id)
        available_tables = [m.get("table_name") for m in self._metadatas.get(db_id, []) if m.get("table_name")]
        return detect_exact_table_mentions(query, available_tables)

    def retrieve(self, query: str, top_k: int = 5, db_id: str = "default") -> list[str]:
        """
        Retrieve relevant schema documents for a specific database using hybrid search.
        Combines ChromaDB vector search with BM25 keyword search (both isolated to db_id),
        then merges via Reciprocal Rank Fusion (RRF), optionally reranks, and boosts/force-includes
        any tables explicitly referenced in the user query.
        """
        start_time = time.perf_counter()

        # Ensure database is indexed
        if db_id not in self._bm25_indices:
            self.index_database(db_id)

        docs_for_db = self._documents.get(db_id, [])
        metas_for_db = self._metadatas.get(db_id, [])
        if not docs_for_db:
            logger.warning("empty_index_for_database", db_id=db_id)
            return []

        # Available tables for this specific database (strict isolation)
        available_tables = [m.get("table_name") for m in metas_for_db if m.get("table_name")]

        # 1. Detect exact table mentions
        exact_mentions = detect_exact_table_mentions(query, available_tables)

        # 2. Adaptive top-k: schemas with >= 20 tables have higher collision risk
        total_tables = len(available_tables)
        effective_top_k = top_k
        if total_tables >= 20 and effective_top_k < 5:
            effective_top_k = 5
        # If user explicitly mentioned more tables than effective_top_k, expand to fit them
        if len(exact_mentions) > effective_top_k:
            effective_top_k = len(exact_mentions)

        try:
            # 3. Vector search (ChromaDB filtered by db_id)
            vector_docs = self._vector_search(query, effective_top_k, db_id=db_id)

            # 4. Keyword search (BM25 isolated to db_id)
            bm25_docs = self._keyword_search(query, effective_top_k, db_id=db_id)

            # 5. Reciprocal Rank Fusion
            merge_k = effective_top_k * 2 if self._get_reranker() else effective_top_k
            merged = self._rrf_merge(vector_docs, bm25_docs, merge_k)

            if not merged:
                # If neither returned a match, return first few docs
                merged = docs_for_db[:effective_top_k]

            # 6. Cross-encoder reranking (optional)
            reranked = False
            reranker = self._get_reranker()
            if reranker and len(merged) > effective_top_k:
                merged = self._rerank(query, merged, effective_top_k)
                reranked = True

            # 7. Exact table boosting / forced inclusion
            # Build quick map of table_name -> document
            doc_by_table = {m["table_name"]: doc for m, doc in zip(metas_for_db, docs_for_db) if "table_name" in m}
            priority_docs = [doc_by_table[t] for t in exact_mentions if t in doc_by_table]

            # Preserve normal ranking for all other tables
            seen_docs = set(priority_docs)
            remaining_docs = [doc for doc in merged if doc not in seen_docs]

            result = (priority_docs + remaining_docs)[:effective_top_k]

            # Extract table names for logging
            retrieved_tables = []
            for doc in result:
                for line in doc.split("\n"):
                    if line.startswith("Table: "):
                        retrieved_tables.append(line.replace("Table: ", "").strip())
                        break

            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # Structured logging for retrieval quality
            logger.info(
                "retrieval_complete",
                db_id=db_id,
                question=query[:80],
                dense_result_count=len(vector_docs),
                bm25_result_count=len(bm25_docs),
                rrf_result_count=len(merged),
                final_result_count=len(result),
                retrieved_tables=retrieved_tables,
                exact_table_mentions=exact_mentions,
                effective_top_k=effective_top_k,
                retrieval_latency_ms=latency_ms,
                reranked=reranked,
            )

            return result

        except Exception as e:
            logger.error("retrieval_failed", db_id=db_id, error=str(e))
            return []

    def retrieve_expanded(self, query: str, entities: list[str] = None, top_k: int = 5, db_id: str = "default") -> list[str]:
        """
        Retrieve with query expansion for multi-table queries, scoped to db_id.
        """
        if not entities or len(entities) <= 1:
            return self.retrieve(query, top_k=top_k, db_id=db_id)

        try:
            expanded_queries = self._expand_query(query, entities)
            all_docs = []
            seen_hashes = set()

            for q in expanded_queries:
                docs = self.retrieve(q, top_k=top_k, db_id=db_id)
                for doc in docs:
                    doc_hash = hash(doc[:200])
                    if doc_hash not in seen_hashes:
                        seen_hashes.add(doc_hash)
                        all_docs.append(doc)

            reranker = self._get_reranker()
            if reranker and len(all_docs) > top_k:
                all_docs = self._rerank(query, all_docs, top_k)

            result = all_docs[:top_k]

            logger.info(
                "expanded_retrieval_complete",
                db_id=db_id,
                num_queries=len(expanded_queries),
                total_candidates=len(all_docs),
                returned=len(result),
            )
            return result
        except Exception as e:
            logger.warning("expanded_retrieval_failed", db_id=db_id, error=str(e))
            return self.retrieve(query, top_k=top_k, db_id=db_id)

    @staticmethod
    def _expand_query(query: str, entities: list[str]) -> list[str]:
        """
        Generate multiple search queries for better recall.
        1. Original query
        2. Per-entity queries
        3. Cross-entity relationship query
        """
        queries = [query]
        for entity in entities:
            queries.append(f"{entity} table schema columns relationships")
        if len(entities) > 1:
            queries.append(f"relationship between {' and '.join(entities)} foreign key join")
        return queries

    def _vector_search(self, query: str, top_k: int, db_id: str = "default") -> list[str]:
        """ChromaDB semantic similarity search restricted to the specified db_id."""
        if not self.vector_enabled or self.chroma_client is None:
            return []

        try:
            col = self._get_or_create_collection(db_id)
            count = col.count()
            if count == 0:
                return []
            results = col.query(
                query_texts=[query],
                n_results=min(top_k, count),
                where={"db_id": db_id},
            )
            return results["documents"][0] if results.get("documents") else []
        except Exception as e:
            logger.warning("vector_search_failed", db_id=db_id, error=str(e))
            return []

    def _keyword_search(self, query: str, top_k: int, db_id: str = "default") -> list[str]:
        """BM25 keyword search restricted to the specified db_id."""
        bm25 = self._bm25_indices.get(db_id)
        docs = self._documents.get(db_id, [])
        if not bm25 or not docs:
            return []

        try:
            tokenized_query = query.lower().split()
            scores = bm25.get_scores(tokenized_query)

            top_indices = sorted(
                range(len(scores)),
                key=lambda i: scores[i],
                reverse=True,
            )[:top_k]

            return [docs[i] for i in top_indices if scores[i] > 0]
        except Exception as e:
            logger.warning("bm25_search_failed", db_id=db_id, error=str(e))
            return []

    @staticmethod
    def _rrf_merge(list_a: list[str], list_b: list[str], top_k: int, k: int = 60) -> list[str]:
        """
        Reciprocal Rank Fusion — merges two ranked lists.
        RRF score = Σ 1/(k + rank) for each list the document appears in.
        k=60 is the standard constant from the original RRF paper.
        """
        scores: dict[str, float] = {}

        for rank, doc in enumerate(list_a):
            scores[doc] = scores.get(doc, 0) + 1.0 / (k + rank + 1)

        for rank, doc in enumerate(list_b):
            scores[doc] = scores.get(doc, 0) + 1.0 / (k + rank + 1)

        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in sorted_docs[:top_k]]

    def _get_reranker(self):
        """Lazy-load the cross-encoder reranker."""
        if os.environ.get("DISABLE_ML_INTENT", "false").lower() in ("true", "1", "yes") or not self.vector_enabled:
            return None

        if self._reranker_loaded:
            return self._reranker

        self._reranker_loaded = True
        try:
            from sentence_transformers import CrossEncoder
            self._reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            logger.info("cross_encoder_reranker_loaded")
        except ImportError:
            logger.info("reranker_unavailable", hint="pip install sentence-transformers for reranking")
        except Exception as e:
            logger.warning("reranker_load_failed", error=str(e))

        return self._reranker

    def _rerank(self, query: str, docs: list[str], top_k: int) -> list[str]:
        """Rerank documents using cross-encoder for precise relevance scoring."""
        try:
            pairs = [(query, doc) for doc in docs]
            scores = self._reranker.predict(pairs)
            ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
            return [doc for doc, _ in ranked[:top_k]]
        except Exception as e:
            logger.warning("reranking_failed", error=str(e))
            return docs[:top_k]

    def refresh_index(self, db_id: str = "default"):
        """Re-index a database schema."""
        logger.info("reindexing_schema", db_id=db_id)
        if db_id == "default" and hasattr(self.db_pool, 'clear_schema_cache'):
            self.db_pool.clear_schema_cache()
        self.index_database(db_id, force=True)

    # ── Business Knowledge RAG Methods ─────────────────────────────

    def index_business_definitions(
        self,
        db_id: str = "default",
        definitions: Optional[list[Any]] = None,
        force: bool = False,
    ) -> int:
        """
        Index business definitions for a specific database into ChromaDB and BM25.
        Strictly partitions by db_id.
        """
        if definitions is None:
            from app.semantics.registry import get_semantic_registry
            reg = get_semantic_registry()
            definitions = reg.get_business_definitions(db_id)

        if not definitions:
            return 0

        self._business_defs[db_id] = definitions

        documents = []
        metadatas = []
        ids = []

        for bdef in definitions:
            term = bdef.term.lower().strip()
            synonyms_str = ", ".join(bdef.synonyms) if getattr(bdef, "synonyms", None) else ""
            cols_str = ", ".join(bdef.preferred_columns) if getattr(bdef, "preferred_columns", None) else ""
            formula_str = bdef.formula_or_hint or ""
            examples_str = ", ".join(bdef.examples) if getattr(bdef, "examples", None) else ""
            related_str = ", ".join(bdef.related_terms) if getattr(bdef, "related_terms", None) else ""

            doc_text = (
                f"Business Term: {bdef.term}\n"
                f"Meaning: {bdef.meaning}\n"
                f"Preferred semantic columns: {cols_str}\n"
                f"Formula hint: {formula_str}\n"
                f"Synonyms: {synonyms_str}\n"
                f"Examples: {examples_str}\n"
                f"Related Terms: {related_str}\n"
                f"Database: {db_id}"
            )
            documents.append(doc_text)
            meta = {
                "db_id": db_id,
                "document_type": "business_definition",
                "term": term,
                "source": getattr(bdef, "source", "enterprise_glossary"),
                "priority": getattr(bdef, "priority", 100),
            }
            metadatas.append(meta)
            ids.append(f"bdef_{db_id}_{term.replace(' ', '_')}")

        self._business_documents[db_id] = documents
        self._business_metadatas[db_id] = metadatas

        # Vector indexing in ChromaDB
        if self.vector_enabled and self.chroma_client:
            collection = self._get_or_create_collection(db_id)
            try:
                # Upsert or clear existing bdef ids then add
                if hasattr(collection, "upsert"):
                    collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
                else:
                    try:
                        collection.delete(ids=ids)
                    except Exception:
                        pass
                    collection.add(documents=documents, metadatas=metadatas, ids=ids)
            except Exception as e:
                logger.warning("chroma_business_add_failed", db_id=db_id, error=str(e))

        # BM25 keyword index
        tokenized = [doc.lower().split() for doc in documents]
        self._business_bm25_indices[db_id] = BM25Okapi(tokenized)

        logger.info(
            "business_definitions_indexed",
            db_id=db_id,
            count=len(definitions),
        )
        return len(definitions)

    def retrieve_business_knowledge(
        self,
        query: str,
        top_k: int = 3,
        db_id: str = "default",
    ) -> list[Any]:
        """
        Retrieve relevant BusinessDefinition objects for a query using hybrid search.
        Strictly enforces db_id isolation.
        """
        if db_id not in self._business_bm25_indices or not self._business_defs.get(db_id):
            self.index_business_definitions(db_id=db_id)

        all_defs = self._business_defs.get(db_id, [])
        if not all_defs:
            return []

        q_lower = query.lower()

        # 1. Exact term or synonym match boost
        exact_matches = []
        seen_terms = set()
        for d in all_defs:
            d_term = d.term.lower().strip()
            # Match word boundary
            if re.search(rf"\b{re.escape(d_term)}\b", q_lower):
                exact_matches.append(d)
                seen_terms.add(d_term)
                continue
            # Check synonyms
            for syn in getattr(d, "synonyms", []):
                syn_low = syn.lower().strip()
                if syn_low and re.search(rf"\b{re.escape(syn_low)}\b", q_lower):
                    exact_matches.append(d)
                    seen_terms.add(d_term)
                    break

        # 2. Vector search in ChromaDB
        vector_docs = []
        if self.vector_enabled and self.chroma_client:
            try:
                col = self._get_or_create_collection(db_id)
                if col.count() > 0:
                    res = col.query(
                        query_texts=[query],
                        n_results=min(top_k * 2, col.count()),
                        where={"db_id": db_id},
                    )
                    if res.get("documents") and res.get("metadatas"):
                        for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                            if meta.get("document_type") == "business_definition":
                                vector_docs.append(doc)
            except Exception as e:
                logger.debug("vector_business_search_failed", db_id=db_id, error=str(e))

        # 3. BM25 keyword search
        bm25 = self._business_bm25_indices.get(db_id)
        docs = self._business_documents.get(db_id, [])
        bm25_docs = []
        if bm25 and docs:
            try:
                tokens = q_lower.split()
                scores = bm25.get_scores(tokens)
                top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k * 2]
                bm25_docs = [docs[i] for i in top_idx if scores[i] > 0]
            except Exception as e:
                logger.debug("bm25_business_search_failed", db_id=db_id, error=str(e))

        # 4. RRF merge
        merged_docs = self._rrf_merge(vector_docs, bm25_docs, top_k * 2)

        # Map merged docs back to BusinessDefinition objects
        doc_to_def = {}
        for d, doc in zip(self._business_defs.get(db_id, []), self._business_documents.get(db_id, [])):
            doc_to_def[doc] = d

        ranked_defs = []
        for doc in merged_docs:
            if doc in doc_to_def:
                bdef = doc_to_def[doc]
                t_key = bdef.term.lower().strip()
                if t_key not in seen_terms:
                    ranked_defs.append(bdef)
                    seen_terms.add(t_key)

        # Combine: exact matches first, then hybrid retrieved
        combined = (exact_matches + ranked_defs)[:top_k]
        return combined
