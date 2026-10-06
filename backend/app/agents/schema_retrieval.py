"""
Schema Retrieval Agent — Fetches relevant schema context using hybrid RAG.
Uses both vector similarity and keyword search to find the best matching tables.
retrieval_top_k is provided by the query_understanding agent and controls
how many schema documents are fetched (3=simple, 5=default, 8=complex).
"""

import structlog

from app.agents.state import AgentState

logger = structlog.get_logger()

# Valid top_k values — anything outside this range is clamped to the default.
_VALID_TOP_K = frozenset({3, 5, 8})
_DEFAULT_TOP_K = 5


def _compress_schema_to_ddl(raw_schema: str) -> str:
    """
    Compress verbose schema text into compact DDL-like format.

    Input format (from SchemaEnricher/get_full_schema):
        Table: employees
        Columns:
          - id (int) [PRIMARY KEY]
          - name (varchar(100))
          - salary (decimal(10,2))
          - department_id (int) [FOREIGN KEY]
        Relationships:
          - department_id → departments.id

    Output format:
        TABLE employees (id INT PK, name VARCHAR, salary DECIMAL, department_id INT FK)
        FK: employees.department_id → departments.id

    ~70% smaller, unambiguous column names for the LLM.
    """
    import re

    lines = raw_schema.split("\n")
    tables = []
    current_table = None
    columns = []
    fks = []

    for line in lines:
        stripped = line.strip()

        # Table header
        if stripped.startswith("Table: "):
            # Flush previous table
            if current_table and columns:
                tables.append(_format_ddl_table(current_table, columns, fks))
            current_table = stripped.replace("Table: ", "").strip()
            columns = []
            fks = []

        # Column definition
        elif stripped.startswith("- ") and "(" in stripped:
            col_match = re.match(
                r'-\s+(\w+)\s+\(([^)]+)\)\s*(.*)', stripped
            )
            if col_match:
                col_name = col_match.group(1)
                col_type = col_match.group(2).split("(")[0].upper()  # INT, VARCHAR, etc.
                flags = col_match.group(3)

                suffix = ""
                if "PRIMARY KEY" in flags:
                    suffix = " PK"
                elif "FOREIGN KEY" in flags:
                    suffix = " FK"
                columns.append(f"{col_name} {col_type}{suffix}")

        # Relationship line
        elif "→" in stripped and current_table:
            fk_match = re.match(r'-?\s*(\w+)\s*→\s*(\w+)\.(\w+)', stripped)
            if fk_match:
                fks.append(
                    f"{current_table}.{fk_match.group(1)} → {fk_match.group(2)}.{fk_match.group(3)}"
                )

    # Flush last table
    if current_table and columns:
        tables.append(_format_ddl_table(current_table, columns, fks))

    return "\n".join(tables) if tables else raw_schema  # Fallback if parsing fails


def _format_ddl_table(table_name: str, columns: list[str], fks: list[str]) -> str:
    """Format a single table as DDL-like string."""
    cols_str = ", ".join(columns)
    result = f"TABLE {table_name} ({cols_str})"
    if fks:
        result += "\n" + "\n".join(f"  FK: {fk}" for fk in fks)
    return result

def schema_retrieval_node(state: AgentState, rag_retriever, db_pool) -> dict:
    """
    Retrieve relevant database schema context for SQL generation.
    Uses hybrid search (vector + BM25) for best results.

    Reads retrieval_top_k from state (set by query_understanding agent).
    Falls back to 5 if the field is absent or invalid.
    """
    user_query = state["user_query"]
    entities = state.get("entities", [])
    route_intent = state.get("route_intent", state.get("intent", "data_query"))
    trace_id = state.get("trace_id", "unknown")

    # ── Resolve top_k from state with fallback ───────────
    raw_top_k = state.get("retrieval_top_k", _DEFAULT_TOP_K)
    top_k = raw_top_k if raw_top_k in _VALID_TOP_K else _DEFAULT_TOP_K

    # Adaptive top_k for large databases (>= 20 tables) to prevent compound-table truncation
    top_k_source = "state" if raw_top_k in _VALID_TOP_K else "default_fallback"
    try:
        if db_pool and hasattr(db_pool, "get_tables"):
            total_tables = len(db_pool.get_tables())
            if total_tables >= 20 and top_k < 5:
                top_k = 5
                top_k_source = "adaptive_large_schema"
    except Exception:
        pass

    logger.info(
        "agent_started",
        agent="schema_retrieval",
        trace_id=trace_id,
        retrieval_top_k=top_k,
        top_k_source=top_k_source,
        complexity=state.get("complexity", "unknown"),
    )

    try:
        # ── Meta query: return full schema ───────────────
        if route_intent == "meta_query":
            full_schema = db_pool.get_full_schema()
            tables = db_pool.get_tables()
            logger.info(
                "schema_retrieved",
                trace_id=trace_id,
                source="meta_query",
                tables_found=len(tables),
                top_k_used=None,
            )
            return {
                "relevant_schema": full_schema,
                "relevant_tables": tables,
                "retrieval_source": "meta_query",
            }

        # ── Database-Aware Hybrid RAG retrieval ─────────────────
        db_id = state.get("db_id") or "default"
        search_query = user_query
        if entities:
            search_query += " " + " ".join(entities)

        retrieved_docs = []
        if rag_retriever:
            try:
                # Use expanded retrieval for multi-table queries (better recall)
                if len(entities) >= 2 and hasattr(rag_retriever, 'retrieve_expanded'):
                    retrieved_docs = rag_retriever.retrieve_expanded(
                        search_query, entities=entities, top_k=top_k, db_id=db_id
                    )
                else:
                    retrieved_docs = rag_retriever.retrieve(
                        search_query, top_k=top_k, db_id=db_id
                    )
            except TypeError:
                # Mock retriever in tests that doesn't accept db_id
                retrieved_docs = rag_retriever.retrieve(search_query, top_k=top_k)
            except Exception as e:
                logger.warning("rag_retrieval_failed_fallback_to_schema", error=str(e), db_id=db_id)
                retrieved_docs = []

        if not retrieved_docs:
            # Fallback: return full schema when RAG returns nothing or index not ready
            logger.warning(
                "rag_empty_results",
                trace_id=trace_id,
                db_id=db_id,
                top_k_requested=top_k,
                fallback="full_schema",
            )
            full_schema = db_pool.get_full_schema()
            return {
                "relevant_schema": _compress_schema_to_ddl(full_schema),
                "relevant_tables": db_pool.get_tables(),
                "retrieval_source": f"fallback_full_schema:{db_id}",
            }

        # Extract table names from retrieved documents.
        relevant_tables = []
        for doc in retrieved_docs:
            for line in doc.split("\n"):
                if line.startswith("Table: "):
                    table_name = line.replace("Table: ", "").strip()
                    if table_name not in relevant_tables:
                        relevant_tables.append(table_name)

        # Compress schema to DDL format for smaller prompt context
        raw_schema = "\n\n".join(retrieved_docs)
        relevant_schema = _compress_schema_to_ddl(raw_schema)

        logger.info(
            "schema_retrieved",
            trace_id=trace_id,
            db_id=db_id,
            source=f"hybrid_rag:{db_id}",
            top_k_requested=top_k,
            docs_returned=len(retrieved_docs),
            tables_found=len(relevant_tables),
            tables=relevant_tables,
            compression_ratio=round(len(relevant_schema) / max(len(raw_schema), 1), 2),
        )

        # Phase 9 & 10: Business Semantic Layer & Interactive Disambiguation
        semantic_context = ""
        ambiguities_data = []
        active_clarification = None
        requires_clarification = False
        semantic_assumptions = []
        friendly_message = state.get("friendly_message")

        try:
            try:
                from app.semantics.registry import get_semantic_registry
                from app.semantics.models import SemanticAmbiguity
                from app.semantics.ambiguity_detector import AmbiguityDetector
            except ImportError:
                from backend.app.semantics.registry import get_semantic_registry
                from backend.app.semantics.models import SemanticAmbiguity
                from backend.app.semantics.ambiguity_detector import AmbiguityDetector

            # Phase 11: Business Knowledge RAG Retrieval
            retrieved_bdefs = []
            if rag_retriever and hasattr(rag_retriever, "retrieve_business_knowledge"):
                try:
                    retrieved_bdefs = rag_retriever.retrieve_business_knowledge(user_query, top_k=3, db_id=db_id)
                except Exception as e:
                    logger.debug("business_knowledge_retrieval_failed", error=str(e), db_id=db_id)

            sem_reg = get_semantic_registry()
            semantic_context = sem_reg.get_semantic_context(
                query=user_query,
                db_id=db_id,
                relevant_tables=relevant_tables,
                pool=db_pool,
                retrieved_business_defs=retrieved_bdefs,
            )

            # Phase 10: Interactive Clarification Response Resolution
            pending_clarification = state.get("active_clarification")
            clarification_resp = state.get("clarification_response")

            if pending_clarification and clarification_resp:
                # User has provided an answer to a previous clarification prompt
                amb_obj = SemanticAmbiguity(**pending_clarification)
                resolved_cand = sem_reg.resolve_clarification(clarification_resp, amb_obj)
                if resolved_cand:
                    logger.info("clarification_successfully_resolved", trace_id=trace_id, candidate=resolved_cand.candidate_id)
                    semantic_context = sem_reg.apply_clarification(resolved_cand, semantic_context)
                    cand_data = resolved_cand.model_dump() if hasattr(resolved_cand, "model_dump") else resolved_cand.dict()

                    # Phase 11: Persistent Semantic Learning Event Recording
                    try:
                        try:
                            from app.semantics.learning import get_semantic_learning_manager
                        except ImportError:
                            from backend.app.semantics.learning import get_semantic_learning_manager
                        get_semantic_learning_manager().record_clarification_event(
                            database_id=db_id,
                            original_term=amb_obj.concept,
                            selected_candidate=resolved_cand.column_name,
                            rejected_candidates=[c.column_name for c in amb_obj.candidates if c.candidate_id != resolved_cand.candidate_id],
                            user_confirmation=clarification_resp,
                            confidence=resolved_cand.confidence,
                        )
                    except Exception as e:
                        logger.debug("record_learning_event_failed", error=str(e))

                    return {
                        "relevant_schema": relevant_schema,
                        "relevant_tables": relevant_tables,
                        "retrieval_source": f"rag_top_k:{top_k}",
                        "semantic_context": semantic_context,
                        "requires_clarification": False,
                        "clarification_resolved": True,
                        "selected_candidate": cand_data,
                        "active_clarification": None,
                    }
                else:
                    # User response was ambiguous or invalid - safely re-prompt
                    logger.warning("clarification_response_unrecognized", trace_id=trace_id, resp=str(clarification_resp)[:30])
                    reprompt_msg = (
                        f"I could not match '{clarification_resp}' to the available options.\n\n"
                        + AmbiguityDetector.format_user_clarification_message(amb_obj)
                    )
                    return {
                        "relevant_schema": relevant_schema,
                        "relevant_tables": relevant_tables,
                        "retrieval_source": f"rag_top_k:{top_k}",
                        "semantic_context": semantic_context,
                        "requires_clarification": True,
                        "clarification_resolved": False,
                        "active_clarification": pending_clarification,
                        "friendly_message": reprompt_msg,
                    }

            # Phase 10: Ambiguity Detection & Confidence Evaluation
            detected = sem_reg.detect_ambiguities(
                query=user_query,
                db_id=db_id,
                relevant_tables=relevant_tables,
                pool=db_pool,
            )
            for amb in detected:
                amb_dict = amb.model_dump() if hasattr(amb, "model_dump") else amb.dict()
                ambiguities_data.append(amb_dict)
                if amb.requires_clarification and not active_clarification:
                    active_clarification = amb_dict
                    requires_clarification = True
                    friendly_message = AmbiguityDetector.format_user_clarification_message(amb)
                    logger.info(
                        "clarification_requested",
                        trace_id=trace_id,
                        ambiguity_type=amb.ambiguity_type.value,
                        concept=amb.concept,
                        candidate_count=len(amb.candidates),
                    )
                elif not amb.requires_clarification and amb.candidates:
                    top_cand = amb.candidates[0]
                    semantic_assumptions.append({
                        "ambiguity_type": amb.ambiguity_type.value,
                        "concept": amb.concept,
                        "selected_column": top_cand.column_name,
                        "table_name": top_cand.table_name,
                        "semantic_role": top_cand.semantic_role.value,
                        "confidence": amb.top_confidence,
                        "reason": amb.reason,
                        "alternatives_considered": [c.column_name for c in amb.candidates[1:]],
                    })

        except Exception as e:
            logger.debug("semantic_context_failed", error=str(e), db_id=db_id)

        result_dict = {
            "relevant_schema": relevant_schema,
            "relevant_tables": relevant_tables,
            "retrieval_source": f"rag_top_k:{top_k}",
            "semantic_context": semantic_context,
            "ambiguities": ambiguities_data,
            "active_clarification": active_clarification,
            "requires_clarification": requires_clarification,
            "semantic_assumptions": semantic_assumptions,
        }
        if friendly_message is not None:
            result_dict["friendly_message"] = friendly_message

        return result_dict


    except Exception as e:
        logger.error("schema_retrieval_failed", trace_id=trace_id, error=str(e))
        # Graceful fallback — never crash the pipeline over a retrieval failure.
        try:
            full_schema = db_pool.get_full_schema()
            return {
                "relevant_schema": full_schema,
                "relevant_tables": db_pool.get_tables(),
                "retrieval_source": "full_schema_fallback",
            }
        except Exception:
            return {
                "relevant_schema": "Schema unavailable",
                "relevant_tables": [],
                "retrieval_source": "error",
                "error": f"Schema retrieval failed: {str(e)}",
                "error_agent": "schema_retrieval",
            }

