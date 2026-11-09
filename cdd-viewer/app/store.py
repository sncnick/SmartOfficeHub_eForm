"""Document store. MongoDB when CDD_MONGO_URL is set, otherwise memory."""

from __future__ import annotations

import copy
import os
from typing import Any

from .documents import summary

_STORE: "Store | None" = None


class Store:
    def ping(self) -> None:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def list_summaries(self) -> list[dict]:
        raise NotImplementedError

    def get(self, company_id: str) -> dict | None:
        raise NotImplementedError

    def save(self, doc: dict) -> dict:
        raise NotImplementedError

    def delete(self, company_id: str) -> bool:
        raise NotImplementedError

    def replace_all(self, docs: list[dict], meta: dict) -> None:
        raise NotImplementedError

    def get_meta(self) -> dict:
        raise NotImplementedError

    def all_docs(self) -> list[dict]:
        raise NotImplementedError

    def issue_index(self) -> list[dict]:
        rows = []
        for doc in self.all_docs():
            name = summary(doc)
            for issue in doc.get("issues") or []:
                rows.append({
                    "company_id": doc["id"],
                    "company_no": name["company_no"],
                    "title": name["title"],
                    "row": issue.get("row"),
                    "severity": issue.get("severity"),
                    "message": issue.get("message"),
                    "field": issue.get("field"),
                })
        return rows


class MemoryStore(Store):
    def __init__(self) -> None:
        self.companies: dict[str, dict] = {}
        self.meta: dict[str, Any] = {}
        self.pdf_mappings: dict[str, dict] = {}
        self.word_mappings: dict[str, dict] = {}
        self.excel_mappings: dict[str, dict] = {}

    def ping(self) -> None:
        return None

    def count(self) -> int:
        return len(self.companies)

    def list_summaries(self) -> list[dict]:
        return [summary(doc) for doc in self.companies.values()]

    def get(self, company_id: str) -> dict | None:
        doc = self.companies.get(company_id)
        return copy.deepcopy(doc) if doc else None

    def save(self, doc: dict) -> dict:
        self.companies[doc["id"]] = copy.deepcopy(doc)
        return copy.deepcopy(doc)

    def delete(self, company_id: str) -> bool:
        return self.companies.pop(company_id, None) is not None

    def replace_all(self, docs: list[dict], meta: dict) -> None:
        self.companies = {doc["id"]: copy.deepcopy(doc) for doc in docs}
        self.meta = copy.deepcopy(meta)

    def get_meta(self) -> dict:
        return copy.deepcopy(self.meta)

    def all_docs(self) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self.companies.values()]

    def _form_bucket(self, kind: str) -> dict[str, dict]:
        return {"pdf": self.pdf_mappings, "word": self.word_mappings, "excel": self.excel_mappings}[kind]

    def get_form_mapping(self, kind: str, file_name: str) -> dict | None:
        found = self._form_bucket(kind).get(file_name)
        return copy.deepcopy(found) if found else None

    def save_form_mapping(self, kind: str, file_name: str, record: dict, updated_at: str) -> dict:
        stored = {"file": file_name, "updated_at": updated_at, **copy.deepcopy(record)}
        self._form_bucket(kind)[file_name] = stored
        return copy.deepcopy(stored)

    def get_pdf_mapping(self, file_name: str) -> dict | None:
        return self.get_form_mapping("pdf", file_name)

    def save_pdf_mapping(self, file_name: str, maps: dict, ticks: dict, updated_at: str) -> dict:
        return self.save_form_mapping("pdf", file_name, {"maps": maps, "ticks": ticks}, updated_at)


class MongoStore(Store):
    def __init__(self, url: str, database: str) -> None:
        import pymongo

        self._client = pymongo.MongoClient(url, serverSelectionTimeoutMS=4000)
        db = self._client[database]
        self._companies = db["companies"]
        self._meta = db["meta"]
        self._pdf_mappings = db["pdf_mappings"]
        self._word_mappings = db["word_mappings"]
        self._excel_mappings = db["excel_mappings"]

    def ping(self) -> None:
        self._client.admin.command("ping")

    def count(self) -> int:
        return self._companies.count_documents({})

    def list_summaries(self) -> list[dict]:
        return [summary(self._public(doc)) for doc in self._companies.find({})]

    def get(self, company_id: str) -> dict | None:
        found = self._companies.find_one({"_id": company_id})
        return self._public(found) if found else None

    def save(self, doc: dict) -> dict:
        stored = copy.deepcopy(doc)
        stored["_id"] = doc["id"]
        self._companies.replace_one({"_id": doc["id"]}, stored, upsert=True)
        return copy.deepcopy(doc)

    def delete(self, company_id: str) -> bool:
        return self._companies.delete_one({"_id": company_id}).deleted_count > 0

    def replace_all(self, docs: list[dict], meta: dict) -> None:
        self._companies.delete_many({})
        if docs:
            self._companies.insert_many([{**copy.deepcopy(doc), "_id": doc["id"]} for doc in docs])
        self._meta.replace_one({"_id": "workspace"}, {"_id": "workspace", **meta}, upsert=True)

    def get_meta(self) -> dict:
        found = self._meta.find_one({"_id": "workspace"}) or {}
        found.pop("_id", None)
        return found

    def all_docs(self) -> list[dict]:
        return [self._public(doc) for doc in self._companies.find({})]

    def _form_bucket(self, kind: str):
        return {"pdf": self._pdf_mappings, "word": self._word_mappings, "excel": self._excel_mappings}[kind]

    def get_form_mapping(self, kind: str, file_name: str) -> dict | None:
        found = self._form_bucket(kind).find_one({"_id": file_name})
        if not found:
            return None
        found.pop("_id", None)
        return found

    def save_form_mapping(self, kind: str, file_name: str, record: dict, updated_at: str) -> dict:
        stored = {"_id": file_name, "file": file_name, "updated_at": updated_at, **record}
        self._form_bucket(kind).replace_one({"_id": file_name}, stored, upsert=True)
        public = dict(stored)
        public.pop("_id", None)
        return public

    def get_pdf_mapping(self, file_name: str) -> dict | None:
        return self.get_form_mapping("pdf", file_name)

    def save_pdf_mapping(self, file_name: str, maps: dict, ticks: dict, updated_at: str) -> dict:
        return self.save_form_mapping("pdf", file_name, {"maps": maps, "ticks": ticks}, updated_at)

    @staticmethod
    def _public(doc: dict) -> dict:
        cleaned = copy.deepcopy(doc)
        cleaned.pop("_id", None)
        return cleaned


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        url = os.environ.get("CDD_MONGO_URL", "")
        if url and os.environ.get("CDD_STORE") != "memory":
            _STORE = MongoStore(url, os.environ.get("CDD_MONGO_DB", "cdd"))
        else:
            _STORE = MemoryStore()
    return _STORE


def reset_store() -> None:
    global _STORE
    _STORE = None
