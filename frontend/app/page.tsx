"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Incident, Service } from "@/lib/types";

const SEVERITY_COLOURS: Record<string, string> = {
  critical: "bg-red-100 text-red-800",
  high: "bg-orange-100 text-orange-800",
  medium: "bg-yellow-100 text-yellow-800",
  low: "bg-green-100 text-green-800",
};

const ALL_STATUSES = "all";
const STATUS_OPTIONS = [ALL_STATUSES, "open", "acknowledged", "resolved"];

function Badge({ value, colours }: { value: string; colours: Record<string, string> }) {
  const cls = colours[value.toLowerCase()] ?? "bg-gray-100 text-gray-800";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {value}
    </span>
  );
}

function summarySnippet(incident: Incident): string {
  if (!incident.llm_summary) return "—";
  try {
    const parsed = JSON.parse(incident.llm_summary);
    const text: string = parsed.summary ?? "";
    return text.length > 120 ? text.slice(0, 117) + "…" : text;
  } catch {
    return "—";
  }
}

type IncidentResult = { key: string; incidents: Incident[]; error: string | null };

export default function IncidentListPage() {
  const [status, setStatus] = useState("open");
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [services, setServices] = useState<string[] | null>(null);
  const [result, setResult] = useState<IncidentResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/services")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("Request failed"))))
      .then((data: Service[]) => {
        if (!cancelled) setServices(data.map((s) => s.affected_service));
      })
      // The tag row is an enhancement over the search box, so a failure here
      // degrades to "no tags" rather than breaking the incident list.
      .catch(() => {
        if (!cancelled) setServices([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const matchingServices = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = services ?? [];
    return q ? all.filter((name) => name.toLowerCase().includes(q)) : all;
  }, [services, query]);

  // Typing the full name selects it, so partial or wrong-case input narrows the
  // tags instead of firing an exact-match query that returns nothing.
  const typedExactMatch = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    return (services ?? []).find((name) => name.toLowerCase() === q) ?? null;
  }, [services, query]);

  const selectedService = picked ?? typedExactMatch;
  const filterKey = `${status}|${selectedService ?? ""}`;

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ status });
    if (selectedService) params.set("service", selectedService);

    // State is only set from async continuations — never synchronously during
    // the effect — and stale responses are dropped so fast filter switching
    // cannot render the wrong result set.
    fetch(`/api/incidents?${params}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("Request failed"))))
      .then((incidents: Incident[]) => {
        if (!cancelled) setResult({ key: filterKey, incidents, error: null });
      })
      .catch(() => {
        if (!cancelled) {
          setResult({ key: filterKey, incidents: [], error: "Failed to load incidents." });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [filterKey, status, selectedService]);

  const loading = result?.key !== filterKey;
  const incidents = result?.incidents ?? [];
  const error = result?.error ?? null;

  function toggleService(name: string) {
    if (selectedService === name) {
      setPicked(null);
      setQuery("");
    } else {
      setPicked(name);
      setQuery(name);
    }
  }

  return (
    <main className="max-w-5xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Incident Dashboard</h1>

      <div className="flex flex-wrap gap-4 mb-4">
        <div>
          <label htmlFor="status" className="block text-sm font-medium text-gray-700 mb-1">
            Status
          </label>
          <select
            id="status"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="service" className="block text-sm font-medium text-gray-700 mb-1">
            Service
          </label>
          <input
            id="service"
            type="text"
            placeholder="Filter services…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPicked(null);
            }}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-56"
          />
        </div>
      </div>

      <div className="mb-6">
        {services === null && <p className="text-sm text-gray-400">Loading services…</p>}
        {services !== null && matchingServices.length === 0 && (
          <p className="text-sm text-gray-500">
            {query.trim() ? `No services match “${query.trim()}”.` : "No services found."}
          </p>
        )}
        {matchingServices.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {matchingServices.map((name) => {
              const active = selectedService === name;
              return (
                <button
                  key={name}
                  type="button"
                  aria-pressed={active}
                  onClick={() => toggleService(name)}
                  className={`px-2.5 py-1 rounded-full border text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                    active
                      ? "bg-blue-600 border-blue-600 text-white"
                      : "bg-white border-gray-300 text-gray-700 hover:bg-gray-50"
                  }`}
                >
                  {name}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {!loading && error && <p className="text-sm text-red-600">{error}</p>}
      {!loading && !error && incidents.length === 0 && (
        <p className="text-sm text-gray-500">No incidents found.</p>
      )}
      {!loading && !error && incidents.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                {["Service", "Severity", "Status", "Created", "Summary"].map((h) => (
                  <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {incidents.map((inc) => (
                <tr key={inc.incident_id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 font-medium text-blue-600 whitespace-nowrap">
                    <Link href={`/incidents/${inc.incident_id}`}>{inc.affected_service}</Link>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <Badge value={inc.severity} colours={SEVERITY_COLOURS} />
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap text-gray-600">{inc.status}</td>
                  <td className="px-4 py-3 whitespace-nowrap text-gray-500">{inc.created_at}</td>
                  <td className="px-4 py-3 text-gray-600 max-w-xs truncate">{summarySnippet(inc)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
