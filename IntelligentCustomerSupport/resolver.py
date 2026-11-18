"""
Knowledge Retrieval and Response Scoring Engine
Searches the approved knowledge base and calculates a confidence match score.
"""

import json
import logging
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any

logger = logging.getLogger(__name__)

def load_knowledge_base(path: str = "knowledge_base.json") -> List[Dict[str, Any]]:
    """Loads and validates approved knowledge base articles from JSON."""
    file_path = Path(path)
    if not file_path.exists():
        logger.error(f"Knowledge base file not found at: {path}")
        return []
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
            if not isinstance(data, list):
                logger.error("Knowledge base format error: root must be a JSON array.")
                return []
            return data
    except json.JSONDecodeError as err:
        logger.error(f"Malformed knowledge base JSON: {err}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error loading knowledge base: {e}")
        return []

def retrieve_article(query_text: str, knowledge_base: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Retrieves the best matching approved article and its heuristic match score.
    Formula: C = 100 * (matched_keywords / total_article_keywords)
    """
    if not query_text or not knowledge_base:
        return None, 0.0

    text = query_text.lower()
    matches = []

    for article in knowledge_base:
        # Strictly enforce safety: only search approved articles
        if not article.get("approved", False):
            continue

        keywords = article.get("keywords", [])
        if not keywords:
            continue

        matched = [
            keyword for keyword in keywords
            if keyword.lower() in text
        ]

        if matched:
            score = (len(matched) / len(keywords)) * 100.0
            matches.append((score, article))

    if not matches:
        return None, 0.0

    # Pick the article with the highest match score
    score, best_article = max(matches, key=lambda item: item[0])
    return best_article, round(score, 2)
