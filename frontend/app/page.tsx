"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Incident } from "@/lib/types";

const SEVERITY_COLOURS: Record<string, string> = {
  critical: "bg-red-100 text-red-800",
  high: "bg-orange-100 text-orange-800",
  medium: "bg-yellow-100 text-yellow-800",
  low: "bg-green-100 text-green-800",
};

const STATUS_OPTIONS = ["open", "acknowledged", "resolved"];

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

export default function IncidentListPage() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [status, setStatus] = useState("open");
  const [service, setService] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);

    const params = new URLSearchParams();
    if (service.trim()) {
      params.set("service", service.trim());
    } else {
      params.set("status", status);
    }

    fetch(`/api/incidents?${params}`)
      .then((r) => {
        if (!r.ok) throw new Error("Request failed");
        return r.json();
      })
      .then(setIncidents)
      .catch(() => setError("Failed to load incidents."))
      .finally(() => setLoading(false));
  }, [status, service]);

  return (
    <main className="max-w-5xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Incident Dashboard</h1>

      <div className="flex flex-wrap gap-4 mb-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Status</label>
          <select
            value={status}
            onChange={(e) => { setStatus(e.target.value); setService(""); }}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Service</label>
          <input
            type="text"
            placeholder="e.g. payments-service"
            value={service}
            onChange={(e) => setService(e.target.value)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-56"
          />
        </div>
      </div>

      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}
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
