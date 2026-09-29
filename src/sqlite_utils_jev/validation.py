"""Validate the small Choice-only interface before publishing a result."""

import hashlib
import json
import math
from typing import Any


class JevError(Exception):
    """An actionable input, budget, transport or recovery error."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def response_json(body: str) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result
    return json.loads(body, object_pairs_hook=unique_object)


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise JevError("Questions must be a nonempty mapping of names to Choice questions")
    for name, question in questions.items():
        if not isinstance(name, str) or not name.strip():
            raise JevError("Question names must be nonempty strings")
        validate_question(question)


def probability(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def validate_question(question: dict) -> None:
    if not isinstance(question, dict) or set(question) != {"type", "instructions", "criteria"}:
        raise JevError("Question needs exactly type, instructions and criteria")
    if question["type"] != "choice":
        raise JevError("This version supports Choice classification only")
    if not isinstance(question["instructions"], str) or not question["instructions"].strip():
        raise JevError("Question instructions must be a nonempty string")
    criteria = question["criteria"]
    if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
        raise JevError("Choice requires 2–255 categories")
    if any(not isinstance(k, str) or not k.strip() or (v is not None and not isinstance(v, str))
           for k, v in criteria.items()):
        raise JevError("Categories must be nonempty strings with string or null descriptions")


def validate_answer(response: Any, question: dict, model: str) -> dict:
    return validate_answers(response, {"classification": question}, model)["classification"]


def validate_answers(response: Any, questions: dict, model: str) -> dict:
    if not isinstance(response, dict) or response.get("model") != model:
        raise JevError("Response model does not match the pinned model")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("Missing or unexpected answer fields")
    for name, question in questions.items():
        validate_choice(answers[name], question)
    return answers


def validate_choice(answer: Any, question: dict) -> None:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevError("Expected a Choice answer")
    selected = answer.get("choice")
    if not isinstance(selected, str) or selected not in question["criteria"]:
        raise JevError("Response contains an unknown category")
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or set(probs) != set(question["criteria"]):
        raise JevError("Response probability categories do not match the question")
    if not all(probability(v) for v in [answer.get("confidence"), *probs.values()]):
        raise JevError("Invalid probability or confidence")
    if abs(sum(probs.values()) - 1) > 0.025:
        raise JevError("Probabilities do not sum to one")
    if probs[selected] < max(probs.values()):
        raise JevError("Selected category is not the highest-probability category")
