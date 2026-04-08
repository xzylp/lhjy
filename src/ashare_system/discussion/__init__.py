"""候选讨论与 case 持久化。"""

from .candidate_case import CandidateCase, CandidateCaseService, CandidateOpinion
from .discussion_service import DiscussionCycle, DiscussionCycleService
from .protocol import (
    DISCUSSION_CONTRACT_VERSION,
    DiscussionAgentPacketsEnvelope,
    DiscussionFinalizePacketEnvelope,
    DiscussionMeetingContextEnvelope,
)

__all__ = [
    "CandidateCase",
    "CandidateCaseService",
    "CandidateOpinion",
    "DiscussionCycle",
    "DiscussionCycleService",
    "DISCUSSION_CONTRACT_VERSION",
    "DiscussionAgentPacketsEnvelope",
    "DiscussionFinalizePacketEnvelope",
    "DiscussionMeetingContextEnvelope",
]
