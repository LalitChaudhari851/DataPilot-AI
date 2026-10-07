"""
Persistent Semantic Learning & Controlled Promotion Layer for DataPilot.
Records user-confirmed clarification events, tracks repetition and confidence thresholds,
and safely creates promotion candidates without prematurely altering enterprise definitions.
"""

import os
import time
import json
import logging
import threading
from typing import Dict, List, Optional, Tuple

from app.semantics.models import (
    SemanticLearningEvent,
    ProposedBusinessDefinition,
    BusinessDefinition,
    BusinessKnowledgeSource,
    DefinitionStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_EVENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "semantics",
    "learning_events.jsonl",
)


class SemanticLearningManager:
    """Thread-safe manager for persistent semantic learning events and safe definition promotion."""

    def __init__(self, persistence_file: Optional[str] = None):
        self.persistence_file = persistence_file or DEFAULT_EVENTS_FILE
        # database_id -> list of SemanticLearningEvent
        self._events: Dict[str, List[SemanticLearningEvent]] = {}
        # database_id -> list of ProposedBusinessDefinition
        self._proposals: Dict[str, List[ProposedBusinessDefinition]] = {}
        self._lock = threading.Lock()
        self._load_persisted_events()

    def _load_persisted_events(self):
        """Load stored learning events on startup if file exists."""
        if not os.path.exists(self.persistence_file):
            return

        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        event = SemanticLearningEvent(**data)
                        self._events.setdefault(event.database_id, []).append(event)
                    except Exception as e:
                        logger.debug("failed_to_parse_learning_event_line", error=str(e))
        except Exception as e:
            logger.warning("could_not_read_learning_events_file", error=str(e))

    def record_clarification_event(
        self,
        database_id: str,
        original_term: str,
        selected_candidate: str,
        rejected_candidates: Optional[List[str]] = None,
        user_confirmation: str = "",
        confidence: float = 1.0,
    ) -> SemanticLearningEvent:
        """Record an explicit user resolution of an ambiguous concept."""
        event = SemanticLearningEvent(
            event_id=f"evt_{int(time.time()*1000)}_{len(self._events.get(database_id, []))}",
            database_id=database_id,
            original_term=original_term.strip().lower(),
            selected_candidate=selected_candidate,
            rejected_candidates=rejected_candidates or [],
            user_confirmation=user_confirmation,
            timestamp=time.time(),
            confidence=confidence,
            source="user_clarification",
        )

        with self._lock:
            self._events.setdefault(database_id, []).append(event)
            # Append to file
            try:
                os.makedirs(os.path.dirname(self.persistence_file), exist_ok=True)
                with open(self.persistence_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event.model_dump()) + "\n")
            except Exception as e:
                logger.warning("failed_to_persist_learning_event", error=str(e))

        logger.info(
            "semantic_learning_event_recorded",
            database_id=database_id,
            term=event.original_term,
            selected=selected_candidate,
            total_events=len(self._events.get(database_id, [])),
        )
        return event

    def get_learning_events(self, database_id: str) -> List[SemanticLearningEvent]:
        """Fetch all learning events for a specific database."""
        with self._lock:
            return list(self._events.get(database_id, []))

    def evaluate_promotion(
        self,
        database_id: str,
        promotion_count: int = 3,
        min_confidence: float = 0.8,
    ) -> List[ProposedBusinessDefinition]:
        """
        Evaluate recorded events for promotion eligibility.
        If a term -> candidate mapping has been confirmed >= promotion_count times
        with average confidence >= min_confidence, generate or update a proposal.
        """
        with self._lock:
            events = self._events.get(database_id, [])
            if not events:
                return []

            # Group by (original_term, selected_candidate)
            grouped: Dict[Tuple[str, str], List[SemanticLearningEvent]] = {}
            for ev in events:
                grouped.setdefault((ev.original_term, ev.selected_candidate), []).append(ev)

            eligible_proposals: List[ProposedBusinessDefinition] = []

            for (term, candidate), ev_list in grouped.items():
                count = len(ev_list)
                avg_conf = sum(e.confidence for e in ev_list) / count

                if count >= promotion_count and avg_conf >= min_confidence:
                    proposal_id = f"prop_{database_id}_{term}_{candidate.replace('.', '_')}"
                    proposal = ProposedBusinessDefinition(
                        proposal_id=proposal_id,
                        database_id=database_id,
                        term=term,
                        meaning=f"User-confirmed preference mapping '{term}' to column '{candidate}'",
                        preferred_columns=[candidate],
                        formula_or_hint=f"{candidate}",
                        confirmation_count=count,
                        confidence=round(avg_conf, 2),
                        source=BusinessKnowledgeSource.USER_CONFIRMED.value,
                        status="PROPOSED",
                        created_at=ev_list[0].timestamp,
                        updated_at=ev_list[-1].timestamp,
                    )
                    eligible_proposals.append(proposal)

            self._proposals[database_id] = eligible_proposals
            return eligible_proposals

    def promote_to_business_definition(
        self,
        proposal: ProposedBusinessDefinition,
    ) -> BusinessDefinition:
        """
        Promote a verified candidate into an active BusinessDefinition.
        Sets source to 'user_confirmed' and priority to 50 (lower than glossary 100).
        """
        bdef = BusinessDefinition(
            term=proposal.term,
            meaning=proposal.meaning,
            database_id=proposal.database_id,
            preferred_columns=proposal.preferred_columns,
            formula_or_hint=proposal.formula_or_hint,
            synonyms=[],
            examples=[],
            related_terms=[],
            source=BusinessKnowledgeSource.USER_CONFIRMED.value,
            priority=50,  # Below explicit enterprise glossary (100)
            confidence=proposal.confidence,
            active=True,
            status=DefinitionStatus.VALID.value,
        )
        proposal.status = "APPROVED"
        logger.info(
            "candidate_promoted_to_business_definition",
            term=proposal.term,
            database_id=proposal.database_id,
            column=proposal.preferred_columns,
        )
        return bdef

    def clear(self, database_id: Optional[str] = None):
        """Clear recorded events and proposals for database or globally."""
        with self._lock:
            if database_id:
                self._events.pop(database_id, None)
                self._proposals.pop(database_id, None)
            else:
                self._events.clear()
                self._proposals.clear()


_learning_manager: Optional[SemanticLearningManager] = None
_learning_lock = threading.Lock()


def get_semantic_learning_manager() -> SemanticLearningManager:
    """Singleton getter for SemanticLearningManager."""
    global _learning_manager
    if _learning_manager is None:
        with _learning_lock:
            if _learning_manager is None:
                _learning_manager = SemanticLearningManager()
    return _learning_manager
