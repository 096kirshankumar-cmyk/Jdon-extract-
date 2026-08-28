# FILE: schema_models.py  (from the external "vision-first" audit, verbatim)
# Kept isolated under proposed_redesign/ so the PRODUCTION pipeline is untouched.
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict, Any


class ImageRef(BaseModel):
    file: str
    source_pages: List[int]


class Option(BaseModel):
    id: Literal["A", "B", "C", "D", "E"]
    text: str
    images: List[ImageRef] = []


class QuestionRecord(BaseModel):
    q_id: str
    subject: str
    chapter_no: int
    q_no: int
    question_text: str
    options: List[Option]
    question_images: List[ImageRef] = []
    tables: List[Dict[str, Any]] = []
    extraction_status: Literal["COMPLETE", "INCOMPLETE"]


class AnswerRecord(BaseModel):
    q_id: str
    q_no: int
    correct_option: Optional[str] = None
    extraction_status: Literal["COMPLETE", "INCOMPLETE"]


class SolutionRecord(BaseModel):
    q_id: str
    q_no: int
    solution_text: str
    tables: List[Dict[str, Any]] = []
    solution_images: List[ImageRef] = []
    extraction_status: Literal["COMPLETE", "INCOMPLETE"]


class ImageManifestRecord(BaseModel):
    q_id: str
    type: Literal["QUESTION", "SOLUTION", "OPTION"]
    option_letter: Optional[str] = None
    file: str
    source_pages: List[int]
    extraction_page: int


class ReviewFlag(BaseModel):
    q_id: str
    reason: str
    severity: Literal["BLOCKER", "REVIEW", "NOISE"]
