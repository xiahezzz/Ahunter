"""Tavily through a host-controlled connection and immutable collection generation.

Candidate tools submit text only. No URL fetching, connection keys, raw provider
parameters, cache enumeration, or provider error text crosses the tool boundary.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import re
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from pydantic import Field

from .contracts import Contract, Name, SearchSettings
from .data.access import unavailable
from .records import Fenced
from .registration import record_identity
from .repository import Missing
from .resolution import digest
from .sources import SourceDocument, SourceRetention


class SearchRequest(Contract):
    query: str = Field(min_length=1, max_length=4000)


class DateMapping(Contract):
    version: Name
    provider_timezone: str | None
    end_date_semantics: Literal["inclusive", "exclusive", "unverified"]

    def map(self, boundary):
        zone = ZoneInfo(boundary.market_timezone)
        requested = boundary.event_cutoff.astimezone(zone).date() - timedelta(days=1)
        reliable = self.provider_timezone == boundary.market_timezone and self.end_date_semantics != "unverified"
        last = requested if reliable else requested - timedelta(days=2)
        end = last + timedelta(days=1) if reliable and self.end_date_semantics == "exclusive" else last
        return {"requested_last_included_date": requested.isoformat(), "last_included_date": requested.isoformat(),
                "end_date": end.isoformat(), "mapping_version": self.version,
                "mapping_status": "fixture_or_connection_verified" if reliable else "conservatively_tightened",
                "market_timezone": boundary.market_timezone, "provider_timezone": self.provider_timezone,
                "end_date_semantics": self.end_date_semantics}


class SearchUnavailable(Exception):
    pass


class SearchTransient(SearchUnavailable):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TavilyHTTPConnection:
    """Resolver is injected by the host connection manager, never by an agent.

No secret is kept in the experiment settings, artifacts or error strings. The
runtime/budget host must authorize a paid request before invoking this transport.
"""
    def __init__(self, resolve_token):
        self._resolve_token = resolve_token

    def search(self, connection_ref, parameters, *, timeout_seconds):
        try:
            token = self._resolve_token(connection_ref)
            if not isinstance(token, str) or not token:
                raise SearchUnavailable()
            request = Request("https://api.tavily.com/search", data=json.dumps(parameters).encode(), method="POST",
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            with build_opener(NoRedirect()).open(request, timeout=timeout_seconds) as response:
                if response.geturl() != "https://api.tavily.com/search":
                    raise SearchUnavailable()
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code < 600:
                raise SearchTransient() from None
            raise SearchUnavailable() from None
        except (URLError, TimeoutError):
            raise SearchTransient() from None
        except SearchUnavailable:
            raise
        except Exception:
            raise SearchUnavailable() from None


def safe_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            and not parsed.username and not parsed.password)


def normalize_results(response, *, last_included_date, market_timezone, max_results):
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise SearchUnavailable()
    # Unexpected automatic or generated output is not accepted as historical evidence.
    if response.get("answer") or response.get("auto_parameters"):
        raise SearchUnavailable()
    last = date.fromisoformat(last_included_date)
    results = []
    for raw in response["results"]:
        if not isinstance(raw, dict) or not safe_url(raw.get("url")):
            continue
        metadata, invalid = {}, False
        for field in ("published_date", "published_at", "publication_date", "updated_at", "updated_date", "last_updated", "date"):
            value = raw.get(field)
            if value is None:
                continue
            try:
                if not isinstance(value, str):
                    raise ValueError()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    day = date.fromisoformat(value)
                else:
                    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    # Date metadata without offset does not prove exact time.
                    day = (moment.astimezone(ZoneInfo(market_timezone)).date() if moment.tzinfo else moment.date())
                if day > last:
                    invalid = True
                metadata[field] = value
            except (ValueError, TypeError):
                invalid = True
        if invalid:
            continue
        result = {"url": raw["url"], "time_metadata": metadata, "time_status": "date_filter_trusted",
                  "content_role": "untrusted_evidence"}
        for field in ("title", "content", "raw_content"):
            if isinstance(raw.get(field), str):
                result[field] = raw[field]
        results.append(result)
        if len(results) >= max_results:
            break
    return results


@dataclass(frozen=True)
class SearchGeneration:
    generation_id: str
    provider_version: str
    retention_policy_ref: str
    retention_seconds: int

    def __post_init__(self):
        if not all(isinstance(x, str) and x for x in (self.generation_id, self.provider_version, self.retention_policy_ref)):
            raise ValueError("search generation identities required")
        if type(self.retention_seconds) is not int or self.retention_seconds <= 0:
            raise ValueError("source retention must be positive seconds")


class HistoricalSearch:
    def __init__(self, session, settings, *, mapping, generation, connection, authorize_call):
        self.session = session
        self.settings = SearchSettings.model_validate(settings)
        self.mapping, self.generation = mapping, generation
        self.connection, self._authorize_call = connection, authorize_call
        self.records = session.records
        self.retention = SourceRetention(self.records)

    def child(self, actor_id):
        return HistoricalSearch(self.session.child(actor_id), self.settings, mapping=self.mapping,
                                generation=self.generation, connection=self.connection, authorize_call=self._authorize_call)

    def _parameters(self, text, mapped):
        return {"query": text, "topic": self.settings.topic, "search_depth": self.settings.depth,
                "max_results": self.settings.max_results, "end_date": mapped["end_date"],
                "auto_parameters": False, "include_answer": False, "include_raw_content": self.settings.include_raw_content,
                "include_images": False, "include_favicon": False}

    def query(self, arguments, *, action_id, replay_only=False):
        self.session.check()
        try:
            request = SearchRequest.model_validate(arguments)
            if not request.query.strip() or re.search(r"(?:[a-z][a-z0-9+.-]*://|www\.)", request.query, re.I):
                raise ValueError()
        except (ValueError, TypeError):
            return self.session.audit({"rejected_request_hash": digest(arguments)}, unavailable("invalid_search_query", required=False), action_id=action_id)
        mapped = self.mapping.map(self.session.scope.boundary)
        parameters = self._parameters(request.query, mapped)
        fingerprint = {"provider": self.settings.provider, "connection_ref": self.settings.connection_ref,
                       "provider_version": self.generation.provider_version, "generation": self.generation.generation_id,
                       "parameters": parameters, "settings": self.settings.model_dump(mode="json"), "mapping": mapped, "boundary": self.session.scope.boundary.model_dump(mode="json"),
                       "trust_policy": self.settings.trust_policy, "retention_policy": self.generation.retention_policy_ref,
                       "retention_seconds": self.generation.retention_seconds}
        key = digest(fingerprint)
        experiment = self.session.scope.experiment_id
        response_id = record_identity(experiment, "search-response", key)
        try:
            cached = self.records.read(response_id)
        except Missing:
            cached = None
        if not self.settings.enabled:
            result = unavailable("search_disabled", required=False)
        elif cached is not None:
            description = self.retention.describe(cached["value"]["source_id"])
            if description["availability"] != "available":
                result = {**unavailable("search_exact_replay_unavailable", required=False), "replay": "metadata_and_hash_only"}
            else:
                result = json.loads(self.retention.read_bytes(cached["value"]["source_id"]))
        elif replay_only:
            result = unavailable("search_cached_response_missing", required=False)
        elif not self.settings.enabled or self.settings.provider != "tavily" or self.connection is None:
            result = unavailable("search_provider_unavailable", required=False)
        elif self.settings.max_results > 20:
            result = unavailable("search_provider_configuration_unsupported", required=False)
        else:
            result = self._collect(key, fingerprint, parameters, mapped)
        return self.session.audit({"product": "search", "query": request.query, "cache_key": key, "mapping": mapped}, result, action_id=action_id,
                                  audit_result={"status": result["status"], "code": result.get("code"),
                                                "response_content_hash": digest(result), "content_retention": "source_policy"})

    def _collect(self, key, fingerprint, parameters, mapped):
        experiment = self.session.scope.experiment_id
        request_id = record_identity(experiment, "search-request", key)
        prepared = self.records.prepare(experiment_id=experiment, kind="search_request", record_id=request_id,
            submission_identity=key, value={"fingerprint": fingerprint})
        with self.records._transaction():
            self.session.check()
            if self.records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (request_id,)).fetchone():
                # A crash, concurrent request or unknown external outcome cannot trigger
                # an implicit refresh of the comparison generation.
                return unavailable("search_outcome_unresolved", required=False)
            self.records._insert(prepared)
        fetched = self.records._now()
        for attempt in range(self.settings.retries + 1):
            self.session.check()
            try:
                self._authorize_call(self.session.scope, key, attempt)
            except Fenced:
                raise
            except Exception:
                return unavailable("search_call_not_authorized", required=False)
            try:
                raw = self.connection.search(self.settings.connection_ref, parameters, timeout_seconds=self.settings.timeout_seconds)
                self.session.check()
                fetched = self.records._now()
                results = normalize_results(raw, last_included_date=mapped["last_included_date"],
                                            market_timezone=mapped["market_timezone"], max_results=self.settings.max_results)
                result = {"status": "available", "results": results, "blocks_scoring": False,
                          "last_included_date": mapped["last_included_date"], "mapping_status": mapped["mapping_status"],
                          "time_status": "date_filter_trusted", "provider_version": self.generation.provider_version,
                          "generation": self.generation.generation_id, "fetched_at": fetched.isoformat(), "replay": "exact_bytes",
                          "index_limitation": "new_queries_may_observe_later_index_changes"}
                artifact = self.records.artifacts.put_json(result)
                source = SourceDocument(source_id="search-response-" + key, content_hash=artifact.content_hash,
                    source_ref=self.settings.connection_ref, title="Historical search response", url=None,
                    fetched_at=fetched, expires_at=fetched + timedelta(seconds=self.generation.retention_seconds),
                    retention_policy_ref=self.generation.retention_policy_ref)
                source_record = self.retention.register(experiment, self.session.scope.test_id, source, submission_identity=key)
                self.records.put(experiment_id=experiment, kind="search_response",
                    record_id=record_identity(experiment, "search-response", key), submission_identity=key,
                    value={"source_id": source_record["record_id"], "response_hash": artifact.content_hash,
                           "attempts": attempt + 1, "fingerprint": fingerprint}, links=(("request", request_id),))
                return result
            except Fenced:
                raise
            except SearchTransient:
                if attempt < self.settings.retries:
                    continue
            except SearchUnavailable:
                pass
            except Exception:
                # Never persist exception text, response bodies or credential errors.
                pass
            return unavailable("search_provider_unavailable", required=False)
