"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";
import {
  ArrowLeftRight,
  ArrowRight,
  Lock,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Trash2,
  Unlock,
  UsersRound,
} from "lucide-react";
import {
  client,
  OperationProgress,
  Peer,
  PeerCardEntry,
  PeerClaim,
  PeerContextResponse,
  PeerCorrectionPlan,
  PeerKind,
} from "@/lib/api";
import { useBank } from "@/lib/bank-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FeatureNotEnabled } from "@/components/feature-not-enabled";

const PEER_KINDS = ["person", "agent", "team", "project", "organization", "other"] as const;
const CARD_CATEGORIES = ["IDENTITY", "ATTRIBUTE", "RELATIONSHIP", "INSTRUCTION"] as const;
type CardCategory = (typeof CARD_CATEGORIES)[number];
const CLAIM_STATUSES = ["active", "contested", "superseded", "retracted"] as const;
type ClaimStatusGroup = (typeof CLAIM_STATUSES)[number];

type PeerForm = {
  id: string;
  displayName: string;
  kind: PeerKind;
  metadata: string;
};

type OperationFeedback = {
  operationId: string;
  observerId: string;
  targetId: string;
  status: BootstrapOperationStatus["status"];
  errorMessage?: string | null;
};

type BootstrapOperationStatus = {
  operation_id: string;
  status: "pending" | "processing" | "completed" | "failed" | "cancelled" | "not_found";
  error_message: string | null;
  retry_count?: number | null;
  next_retry_at?: string | null;
  progress?: OperationProgress | null;
  result_metadata?: Record<string, unknown> | null;
};

type PeerOperationListItem = {
  id: string;
  created_at: string;
  updated_at?: string | null;
  status: string;
  error_message: string | null;
  retry_count?: number | null;
  next_retry_at?: string | null;
  progress?: OperationProgress | null;
};

const EMPTY_PEER_FORM: PeerForm = { id: "", displayName: "", kind: "person", metadata: "" };

function normalizePeers(data: { items?: Peer[]; peers?: Peer[] } | Peer[]): Peer[] {
  if (Array.isArray(data)) return data;
  return data.items ?? data.peers ?? [];
}

function normalizeClaims(data: unknown): PeerClaim[] {
  if (Array.isArray(data)) return data as PeerClaim[];
  if (!data || typeof data !== "object") return [];
  const record = data as { items?: PeerClaim[]; claims?: PeerClaim[] };
  return record.items ?? record.claims ?? [];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function getNestedValue(response: PeerContextResponse | null, key: string): unknown {
  if (!response) return undefined;
  const sources: unknown[] = [response, response.model, response.context];
  for (const source of sources) {
    const record = asRecord(source);
    if (record && key in record) return record[key];
  }
  return undefined;
}

function getEntryText(entry: Record<string, unknown>): string {
  for (const key of ["text", "claim", "value", "content", "name"]) {
    const value = entry[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return JSON.stringify(entry);
}

function getSourceCount(entry: Record<string, unknown>): number | null {
  for (const key of ["source_count", "evidence_count"]) {
    const value = entry[key];
    if (typeof value === "number") return value;
  }
  if (Array.isArray(entry.source_ids)) return entry.source_ids.length;
  if (Array.isArray(entry.sources)) return entry.sources.length;
  if (Array.isArray(entry.evidence)) return entry.evidence.length;
  return null;
}

function getClaimId(entry: Record<string, unknown>): string | null {
  const value = entry.claim_id ?? entry.id;
  return typeof value === "string" && value ? value : null;
}

function isTerminalOperation(status: BootstrapOperationStatus["status"]): boolean {
  return ["completed", "failed", "cancelled", "not_found"].includes(status);
}

function getSourceIds(entry: Record<string, unknown>): string[] {
  const sourceIds = entry.source_ids;
  if (Array.isArray(sourceIds)) {
    return sourceIds.filter((sourceId): sourceId is string => typeof sourceId === "string");
  }
  if (Array.isArray(entry.sources)) {
    return entry.sources
      .map((source: unknown) => {
        const record = asRecord(source);
        return record?.source_id ?? record?.id;
      })
      .filter((sourceId): sourceId is string => typeof sourceId === "string");
  }
  if (Array.isArray(entry.evidence)) {
    return entry.evidence
      .map((source: unknown) => asRecord(source)?.id)
      .filter((sourceId): sourceId is string => typeof sourceId === "string");
  }
  return [];
}

function getMemorySourceIds(entry: Record<string, unknown>): string[] {
  if (!Array.isArray(entry.sources)) return [];
  return entry.sources
    .map((source: unknown) => asRecord(source))
    .filter((source): source is Record<string, unknown> => source?.source_kind === "memory_unit")
    .map((source) => source.source_id)
    .filter((sourceId): sourceId is string => typeof sourceId === "string");
}

function getOrigin(entry: Record<string, unknown>): string | null {
  for (const key of ["origin", "source", "provenance"]) {
    const value = entry[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function getCategory(entry: Record<string, unknown>): CardCategory | null {
  const value = entry.category ?? entry.type ?? entry.claim_type;
  if (typeof value !== "string") return null;
  const normalized = value.toUpperCase();
  return CARD_CATEGORIES.includes(normalized as CardCategory) ? (normalized as CardCategory) : null;
}

function extractCard(response: PeerContextResponse | null): Record<CardCategory, PeerCardEntry[]> {
  const empty = {} as Record<CardCategory, PeerCardEntry[]>;
  for (const category of CARD_CATEGORIES) empty[category] = [];
  const card = getNestedValue(response, "card");
  if (Array.isArray(card)) {
    for (const rawEntry of card) {
      const entry = asRecord(rawEntry);
      if (!entry) continue;
      const category = getCategory(entry) ?? "ATTRIBUTE";
      empty[category].push(entry as PeerCardEntry);
    }
    return empty;
  }

  const cardRecord = asRecord(card);
  if (!cardRecord) return empty;
  if (Array.isArray(cardRecord.entries)) {
    for (const rawEntry of cardRecord.entries) {
      const entry = asRecord(rawEntry);
      if (!entry) continue;
      const category = getCategory(entry) ?? "ATTRIBUTE";
      empty[category].push(entry as PeerCardEntry);
    }
    return empty;
  }
  const entriesRecord = asRecord(cardRecord.entries) ?? cardRecord;
  for (const category of CARD_CATEGORIES) {
    const raw = entriesRecord[category] ?? entriesRecord[category.toLowerCase()];
    if (Array.isArray(raw)) {
      empty[category] = raw
        .map(asRecord)
        .filter((entry): entry is Record<string, unknown> => entry !== null)
        .map((entry) => entry as PeerCardEntry);
    } else if (typeof raw === "string") {
      empty[category] = [{ text: raw }];
    }
  }
  return empty;
}

function formatMetadata(metadata: Record<string, unknown> | null | undefined): string {
  if (!metadata || Object.keys(metadata).length === 0) return "";
  return JSON.stringify(metadata, null, 2);
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function PeersView({ enabled }: { enabled: boolean }) {
  const t = useTranslations("peersView");
  const { currentBank } = useBank();
  // Scope generation: incremented on bank/enabled change and unmount. Every
  // async loader/action captures the generation it started under and discards
  // its state writes, toasts, reloads, and finally-clauses once the scope has
  // moved on. This prevents a late bank-A / pair-A response from overwriting
  // the currently rendered bank-B / pair-B state.
  const scopeGenerationRef = useRef(0);
  const pairRequestRef = useRef(0);
  const pairKeyRef = useRef("");
  const contextPairKeyRef = useRef("");
  const directionalInFlightRef = useRef<{ key: string; requestId: number } | null>(null);
  const mountedRef = useRef(true);
  // Bootstrap polling backpressure: only one list→status chain runs at a time,
  // and a status is committed only when it matches the expected operation.
  const bootstrapPollingRef = useRef(false);
  const refreshPollingRef = useRef(false);
  const expectedBootstrapOperationRef = useRef<string | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [loadingPeers, setLoadingPeers] = useState(false);
  const [peerError, setPeerError] = useState<string | null>(null);
  const [observerId, setObserverId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [peerSearch, setPeerSearch] = useState("");
  const [context, setContext] = useState<PeerContextResponse | null>(null);
  const [claims, setClaims] = useState<PeerClaim[]>([]);
  const [loadingContext, setLoadingContext] = useState(false);
  const [notModeled, setNotModeled] = useState(false);
  const [contextError, setContextError] = useState<string | null>(null);
  const [claimsError, setClaimsError] = useState<string | null>(null);
  const [operation, setOperation] = useState<"model" | "rebuild" | null>(null);
  const [operationFeedback, setOperationFeedback] = useState<OperationFeedback | null>(null);
  const [bootstrapStatus, setBootstrapStatus] = useState<BootstrapOperationStatus | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [submittingBootstrap, setSubmittingBootstrap] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState<BootstrapOperationStatus | null>(null);
  const [refreshHistory, setRefreshHistory] = useState<PeerOperationListItem[]>([]);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [peerCooldownSeconds, setPeerCooldownSeconds] = useState(0);
  const [mutatingClaimId, setMutatingClaimId] = useState<string | null>(null);
  const [memoryToDelete, setMemoryToDelete] = useState<string | null>(null);
  const [deletingMemoryId, setDeletingMemoryId] = useState<string | null>(null);

  const [peerDialogOpen, setPeerDialogOpen] = useState(false);
  const [editingPeer, setEditingPeer] = useState<Peer | null>(null);
  const [peerForm, setPeerForm] = useState<PeerForm>(EMPTY_PEER_FORM);
  const [peerFormError, setPeerFormError] = useState<string | null>(null);
  const [savingPeer, setSavingPeer] = useState(false);

  const [correctionDialogOpen, setCorrectionDialogOpen] = useState(false);
  const [correctionText, setCorrectionText] = useState("");
  const [correctionPlan, setCorrectionPlan] = useState<PeerCorrectionPlan | null>(null);
  const [correctionError, setCorrectionError] = useState<string | null>(null);
  const [savingCorrection, setSavingCorrection] = useState(false);
  // A correction plan belongs to the bank+pair it was generated for. Submitting
  // it against a different scope would apply an old plan to new pair IDs.
  const correctionPlanScopeRef = useRef<{ observerId: string; targetId: string } | null>(null);

  const isCurrentScope = useCallback((generation: number) => {
    return mountedRef.current && generation === scopeGenerationRef.current;
  }, []);

  const pairKey = `${observerId}\u0000${targetId}`;
  pairKeyRef.current = pairKey;

  const isCurrentPairRequest = useCallback(
    (generation: number, expectedPairKey: string, requestId: number) =>
      isCurrentScope(generation) &&
      pairKeyRef.current === expectedPairKey &&
      pairRequestRef.current === requestId,
    [isCurrentScope]
  );

  // Hard reset of every projection when the bank scope changes (or peer
  // modeling is disabled). All async work from the previous scope is
  // invalidated by bumping the generation before the new scope's loaders run.
  useEffect(() => {
    scopeGenerationRef.current += 1;
    pairRequestRef.current += 1;
    pairKeyRef.current = "";
    contextPairKeyRef.current = "";
    directionalInFlightRef.current = null;
    bootstrapPollingRef.current = false;
    refreshPollingRef.current = false;
    expectedBootstrapOperationRef.current = null;
    setPeers([]);
    setLoadingPeers(false);
    setPeerError(null);
    setObserverId("");
    setTargetId("");
    setPeerSearch("");
    setContext(null);
    setClaims([]);
    setLoadingContext(false);
    setNotModeled(false);
    setContextError(null);
    setClaimsError(null);
    setOperation(null);
    setOperationFeedback(null);
    setBootstrapStatus(null);
    setBootstrapError(null);
    setSubmittingBootstrap(false);
    setRefreshStatus(null);
    setRefreshHistory([]);
    setRefreshError(null);
    setPeerCooldownSeconds(0);
    setMutatingClaimId(null);
    setMemoryToDelete(null);
    setDeletingMemoryId(null);
    setCorrectionDialogOpen(false);
    setCorrectionText("");
    setCorrectionPlan(null);
    correctionPlanScopeRef.current = null;
    setCorrectionError(null);
    setSavingCorrection(false);
    setPeerDialogOpen(false);
    setSavingPeer(false);
  }, [currentBank, enabled]);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      scopeGenerationRef.current += 1;
      pairRequestRef.current += 1;
      directionalInFlightRef.current = null;
    };
  }, []);

  const observer = useMemo(() => peers.find((peer) => peer.id === observerId), [peers, observerId]);
  const target = useMemo(() => peers.find((peer) => peer.id === targetId), [peers, targetId]);
  const card = useMemo(() => extractCard(context), [context]);
  const claimsById = useMemo(
    () => new Map(claims.map((claim) => [claim.id, claim] as const)),
    [claims]
  );
  const filteredPeers = useMemo(() => {
    const query = peerSearch.trim().toLowerCase();
    if (!query) return peers;
    return peers.filter((peer) =>
      [peer.display_name, peer.external_id, peer.kind]
        .filter((value): value is string => typeof value === "string")
        .some((value) => value.toLowerCase().includes(query))
    );
  }, [peerSearch, peers]);

  const categoryLabels: Record<CardCategory, string> = {
    IDENTITY: t("category.identity"),
    ATTRIBUTE: t("category.attribute"),
    RELATIONSHIP: t("category.relationship"),
    INSTRUCTION: t("category.instruction"),
  };
  const kindLabels: Record<string, string> = {
    person: t("kind.person"),
    agent: t("kind.agent"),
    team: t("kind.team"),
    project: t("kind.project"),
    organization: t("kind.organization"),
    other: t("kind.other"),
  };
  const statusLabels: Record<ClaimStatusGroup, string> = {
    active: t("status.active"),
    contested: t("status.contested"),
    superseded: t("status.superseded"),
    retracted: t("status.retracted"),
  };

  const loadPeers = useCallback(async () => {
    if (!currentBank || !enabled) return;
    const generation = scopeGenerationRef.current;
    setLoadingPeers(true);
    setPeerError(null);
    try {
      const pageSize = 100;
      const peerMap = new Map<string, Peer>();
      let offset = 0;
      let expectedTotal: number | null = null;
      for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
        const response = await client.listPeers(currentBank, { limit: pageSize, offset });
        if (!isCurrentScope(generation)) return;
        const page = normalizePeers(response);
        if (!Array.isArray(response) && typeof response.total === "number") {
          expectedTotal = response.total;
        }
        const previousSize = peerMap.size;
        for (const peer of page) peerMap.set(peer.id, peer);
        if (
          page.length === 0 ||
          page.length < pageSize ||
          peerMap.size === previousSize ||
          (expectedTotal !== null && peerMap.size >= expectedTotal)
        ) {
          break;
        }
        offset += page.length;
      }
      const nextPeers = [...peerMap.values()];
      if (!isCurrentScope(generation)) return;
      setPeers(nextPeers);
      setObserverId((current) =>
        nextPeers.some((peer) => peer.id === current) ? current : (nextPeers[0]?.id ?? "")
      );
      setTargetId((current) => (nextPeers.some((peer) => peer.id === current) ? current : ""));
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setPeerError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) setLoadingPeers(false);
    }
  }, [currentBank, enabled, isCurrentScope]);

  const loadBootstrapStatus = useCallback(async () => {
    if (!currentBank || !enabled) return;
    if (bootstrapPollingRef.current) return;
    bootstrapPollingRef.current = true;
    const generation = scopeGenerationRef.current;
    try {
      const operations = await client.listOperations(currentBank, {
        type: "peer_bootstrap",
        limit: 1,
      });
      if (!isCurrentScope(generation)) return;
      const latest = operations.operations[0];
      if (!latest) {
        setBootstrapStatus(null);
        setBootstrapError(null);
        return;
      }
      const discoveredId = latest.id;
      // Commit status only for the operation this scope is actually watching.
      // A newer operation discovered between list and status must win.
      const expected = expectedBootstrapOperationRef.current;
      if (expected !== null && discoveredId !== expected) return;
      const status = await client.getOperationStatus(currentBank, discoveredId);
      if (!isCurrentScope(generation)) return;
      // Re-discover before committing: a newer operation may have appeared
      // while the status request was in flight. If the latest operation
      // changed, drop this result and let the next poll pick up the winner.
      const recheck = await client.listOperations(currentBank, {
        type: "peer_bootstrap",
        limit: 1,
      });
      if (!isCurrentScope(generation)) return;
      const latestNow = recheck.operations[0]?.id ?? null;
      if (latestNow !== discoveredId) return;
      if (expected !== null && status.operation_id !== expected) return;
      if (status.operation_id !== discoveredId) return;
      setBootstrapStatus(status);
      setBootstrapError(null);
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setBootstrapError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) bootstrapPollingRef.current = false;
    }
  }, [currentBank, enabled, isCurrentScope]);

  const loadRefreshStatus = useCallback(async () => {
    if (!currentBank || !enabled || refreshPollingRef.current) return;
    refreshPollingRef.current = true;
    const generation = scopeGenerationRef.current;
    try {
      const [operations, config] = await Promise.all([
        client.listOperations(currentBank, { type: "peer_model_refresh", limit: 5 }),
        client.getBankConfig(currentBank),
      ]);
      if (!isCurrentScope(generation)) return;
      const history = operations.operations as PeerOperationListItem[];
      setRefreshHistory(history);
      setPeerCooldownSeconds(Number(config.config.peer_model_cooldown_seconds ?? 0));
      const latest = history[0];
      if (!latest) {
        setRefreshStatus(null);
        setRefreshError(null);
        return;
      }
      const status = await client.getOperationStatus(currentBank, latest.id);
      if (!isCurrentScope(generation)) return;
      const recheck = await client.listOperations(currentBank, {
        type: "peer_model_refresh",
        limit: 1,
      });
      if (!isCurrentScope(generation)) return;
      if (recheck.operations[0]?.id !== latest.id || status.operation_id !== latest.id) return;
      setRefreshStatus(status);
      setRefreshError(null);
    } catch (error) {
      if (isCurrentScope(generation)) setRefreshError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) refreshPollingRef.current = false;
    }
  }, [currentBank, enabled, isCurrentScope]);

  const contextPairMismatchMessage = t("contextPairMismatch");

  const loadDirectionalContext = useCallback(async () => {
    if (!currentBank || !enabled || !observerId || !targetId) {
      pairRequestRef.current += 1;
      directionalInFlightRef.current = null;
      contextPairKeyRef.current = "";
      setContext(null);
      setClaims([]);
      setNotModeled(false);
      setContextError(null);
      setClaimsError(null);
      setLoadingContext(false);
      return;
    }
    const expectedPairKey = `${observerId}\u0000${targetId}`;
    if (directionalInFlightRef.current?.key === expectedPairKey) return;
    const generation = scopeGenerationRef.current;
    const requestId = ++pairRequestRef.current;
    directionalInFlightRef.current = { key: expectedPairKey, requestId };
    setLoadingContext(true);
    setNotModeled(false);
    setContextError(null);
    setClaimsError(null);
    if (contextPairKeyRef.current !== expectedPairKey) {
      setContext(null);
      setClaims([]);
    }
    const [contextResult, claimsResult] = await Promise.allSettled([
      client.getPeerContext(currentBank, observerId, targetId),
      client.getPeerClaims(currentBank, observerId, targetId),
    ]);
    if (!isCurrentPairRequest(generation, expectedPairKey, requestId)) return;
    if (contextResult.status === "fulfilled") {
      const responseObserver = contextResult.value.observer_peer_id;
      const responseTarget = contextResult.value.target_peer_id;
      if (
        (responseObserver && responseObserver !== observerId) ||
        (responseTarget && responseTarget !== targetId)
      ) {
        setContextError(contextPairMismatchMessage);
      } else {
        contextPairKeyRef.current = expectedPairKey;
        setContext(contextResult.value);
      }
    } else if ((contextResult.reason as { status?: number } | undefined)?.status === 404) {
      contextPairKeyRef.current = expectedPairKey;
      setContext(null);
      setNotModeled(true);
    } else {
      setContextError(getErrorMessage(contextResult.reason));
    }
    if (claimsResult.status === "fulfilled") {
      setClaims(normalizeClaims(claimsResult.value));
    } else if ((claimsResult.reason as { status?: number } | undefined)?.status !== 404) {
      setClaimsError(getErrorMessage(claimsResult.reason));
    }
    setLoadingContext(false);
    if (directionalInFlightRef.current?.requestId === requestId) {
      directionalInFlightRef.current = null;
    }
  }, [
    currentBank,
    enabled,
    observerId,
    targetId,
    isCurrentPairRequest,
    contextPairMismatchMessage,
  ]);

  useEffect(() => {
    void loadPeers();
  }, [loadPeers]);

  useEffect(() => {
    void loadBootstrapStatus();
  }, [loadBootstrapStatus]);

  useEffect(() => {
    void loadRefreshStatus();
  }, [loadRefreshStatus]);

  useEffect(() => {
    if (bootstrapStatus?.status !== "pending" && bootstrapStatus?.status !== "processing") return;
    const interval = window.setInterval(() => void loadBootstrapStatus(), 2000);
    return () => window.clearInterval(interval);
  }, [bootstrapStatus?.status, loadBootstrapStatus]);

  useEffect(() => {
    if (bootstrapStatus?.status !== "completed") return;
    void loadPeers();
  }, [bootstrapStatus?.status, loadPeers]);

  useEffect(() => {
    void loadDirectionalContext();
  }, [loadDirectionalContext]);

  useEffect(() => {
    if (refreshStatus?.status !== "pending" && refreshStatus?.status !== "processing") return;
    const interval = window.setInterval(() => {
      void loadRefreshStatus();
      void loadDirectionalContext();
    }, 3000);
    return () => window.clearInterval(interval);
  }, [refreshStatus?.status, loadRefreshStatus, loadDirectionalContext]);

  useEffect(() => {
    if (!operationFeedback || !currentBank) return;
    const expectedPairKey = `${operationFeedback.observerId}\u0000${operationFeedback.targetId}`;
    if (pairKeyRef.current !== expectedPairKey) {
      setOperationFeedback(null);
      setOperation(null);
      return;
    }
    if (isTerminalOperation(operationFeedback.status)) return;
    const generation = scopeGenerationRef.current;
    const operationId = operationFeedback.operationId;
    const timeout = window.setTimeout(() => {
      void client
        .getOperationStatus(currentBank, operationId)
        .then((status) => {
          if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
          setOperationFeedback((current) =>
            current?.operationId === operationId
              ? {
                  ...current,
                  status: status.status,
                  errorMessage: status.error_message,
                }
              : current
          );
          if (isTerminalOperation(status.status)) {
            setOperation(null);
            if (status.status === "completed") void loadDirectionalContext();
          }
        })
        .catch((error: unknown) => {
          if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
          setOperationFeedback((current) =>
            current?.operationId === operationId
              ? { ...current, status: "failed", errorMessage: getErrorMessage(error) }
              : current
          );
          setOperation(null);
        });
    }, 1500);
    return () => window.clearTimeout(timeout);
  }, [operationFeedback, currentBank, isCurrentScope, loadDirectionalContext]);

  const openCreatePeer = () => {
    setEditingPeer(null);
    setPeerForm(EMPTY_PEER_FORM);
    setPeerFormError(null);
    setPeerDialogOpen(true);
  };

  const openEditPeer = (peer: Peer) => {
    setEditingPeer(peer);
    setPeerForm({
      id: peer.external_id,
      displayName: peer.display_name ?? "",
      kind: peer.kind,
      metadata: formatMetadata(peer.metadata),
    });
    setPeerFormError(null);
    setPeerDialogOpen(true);
  };

  const parseMetadata = (): Record<string, unknown> | undefined => {
    if (!peerForm.metadata.trim()) return undefined;
    try {
      const parsed: unknown = JSON.parse(peerForm.metadata);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(t("metadataObjectError"));
      }
      return parsed as Record<string, unknown>;
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : t("metadataJsonError"));
    }
  };

  const savePeer = async () => {
    if (!currentBank || !peerForm.id.trim()) {
      setPeerFormError(t("peerIdRequired"));
      return;
    }
    const generation = scopeGenerationRef.current;
    setSavingPeer(true);
    setPeerFormError(null);
    try {
      const metadata = parseMetadata();
      if (editingPeer) {
        await client.updatePeer(currentBank, editingPeer.id, {
          display_name: peerForm.displayName.trim() || undefined,
          kind: peerForm.kind,
          ...(metadata === undefined ? {} : { metadata }),
        });
        if (!isCurrentScope(generation)) return;
        toast.success(t("peerUpdated"));
      } else {
        await client.createPeer(currentBank, {
          external_id: peerForm.id.trim(),
          display_name: peerForm.displayName.trim() || undefined,
          kind: peerForm.kind,
          ...(metadata === undefined ? {} : { metadata }),
        });
        if (!isCurrentScope(generation)) return;
        toast.success(t("peerCreated"));
      }
      setPeerDialogOpen(false);
      await loadPeers();
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setPeerFormError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) setSavingPeer(false);
    }
  };

  const runModelAction = async (kind: "model" | "rebuild") => {
    if (!currentBank || !observerId || !targetId) return;
    const generation = scopeGenerationRef.current;
    const expectedPairKey = `${observerId}\u0000${targetId}`;
    setOperation(kind);
    setOperationFeedback(null);
    try {
      if (kind === "model") {
        const result = await client.modelPeer(currentBank, observerId, targetId);
        if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
        setOperationFeedback({
          operationId: result.operation_id,
          observerId,
          targetId,
          status: "pending",
        });
        toast.success(t("operationSubmitted"), {
          description: t("operationSubmittedWithId", { id: result.operation_id }),
        });
      } else {
        await client.rebuildPeer(currentBank, observerId, targetId);
        if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
        await loadDirectionalContext();
        if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
        toast.success(t("rebuildOperationFeedback"));
        setOperation(null);
      }
    } catch {
      if (isCurrentScope(generation) && pairKeyRef.current === expectedPairKey) {
        setOperation(null);
      }
      // The API client displays the structured error toast.
    }
  };

  const runBootstrap = async () => {
    if (!currentBank) return;
    const generation = scopeGenerationRef.current;
    setSubmittingBootstrap(true);
    setBootstrapError(null);
    try {
      const result = await client.bootstrapPeers(currentBank);
      if (!isCurrentScope(generation)) return;
      expectedBootstrapOperationRef.current = result.operation_id;
      const status = await client.getOperationStatus(currentBank, result.operation_id);
      if (!isCurrentScope(generation)) return;
      setBootstrapStatus(status);
      toast.success(result.deduplicated ? t("bootstrapAlreadyRunning") : t("bootstrapSubmitted"));
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setBootstrapError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) setSubmittingBootstrap(false);
    }
  };

  const openCorrection = () => {
    setCorrectionText("");
    setCorrectionPlan(null);
    setCorrectionError(null);
    setCorrectionDialogOpen(true);
  };

  const reviewCorrection = async () => {
    if (!currentBank || !observerId || !targetId || !correctionText.trim()) {
      setCorrectionError(t("correctionRequired"));
      return;
    }
    const generation = scopeGenerationRef.current;
    setSavingCorrection(true);
    setCorrectionError(null);
    try {
      const plan = await client.planPeerCorrection(
        currentBank,
        observerId,
        targetId,
        correctionText.trim()
      );
      if (!isCurrentScope(generation)) return;
      setCorrectionPlan(plan);
      correctionPlanScopeRef.current = { observerId, targetId };
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setCorrectionError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) setSavingCorrection(false);
    }
  };

  const submitCorrection = async () => {
    if (!currentBank || !observerId || !targetId || !correctionPlan) return;
    const planScope = correctionPlanScopeRef.current;
    if (
      planScope === null ||
      planScope.observerId !== observerId ||
      planScope.targetId !== targetId
    ) {
      setCorrectionError(t("correctionRequired"));
      return;
    }
    const generation = scopeGenerationRef.current;
    setSavingCorrection(true);
    setCorrectionError(null);
    try {
      await client.createPeerCorrection(currentBank, observerId, targetId, {
        plan: correctionPlan,
      });
      if (!isCurrentScope(generation)) return;
      toast.success(t("correctionSubmitted"));
      setCorrectionDialogOpen(false);
      await loadDirectionalContext();
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      setCorrectionError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) setSavingCorrection(false);
    }
  };

  const toggleClaimLocked = async (claim: PeerClaim) => {
    if (!currentBank || !observerId || !targetId) return;
    const generation = scopeGenerationRef.current;
    setMutatingClaimId(claim.id);
    try {
      await client.updatePeerClaim(currentBank, observerId, targetId, claim.id, !claim.locked);
      if (!isCurrentScope(generation)) return;
      toast.success(claim.locked ? t("claimUnlocked") : t("claimLocked"));
      await loadDirectionalContext();
    } finally {
      if (isCurrentScope(generation)) setMutatingClaimId(null);
    }
  };

  const deleteClaim = async (claim: PeerClaim) => {
    if (!currentBank || !observerId || !targetId || !window.confirm(t("deleteClaimConfirm")))
      return;
    const generation = scopeGenerationRef.current;
    setMutatingClaimId(claim.id);
    try {
      await client.deletePeerClaim(currentBank, observerId, targetId, claim.id);
      if (!isCurrentScope(generation)) return;
      toast.success(t("claimDeleted"));
      await loadDirectionalContext();
    } finally {
      if (isCurrentScope(generation)) setMutatingClaimId(null);
    }
  };

  const deleteMemorySource = async () => {
    if (!currentBank || !memoryToDelete) return;
    const generation = scopeGenerationRef.current;
    const expectedPairKey = `${observerId}\u0000${targetId}`;
    const memoryId = memoryToDelete;
    setDeletingMemoryId(memoryId);
    try {
      await client.deleteMemory(memoryId, currentBank);
      if (!isCurrentScope(generation) || pairKeyRef.current !== expectedPairKey) return;
      toast.success(t("memoryDeleted"));
      setMemoryToDelete(null);
      await loadDirectionalContext();
    } finally {
      if (isCurrentScope(generation)) setDeletingMemoryId(null);
    }
  };

  const claimGroups = useMemo(() => {
    const groups: Record<string, PeerClaim[]> = {
      active: [],
      contested: [],
      superseded: [],
      retracted: [],
    };
    for (const claim of claims) {
      const status = String(claim.status ?? "active").toLowerCase();
      (groups[status] ??= []).push(claim);
    }
    return groups;
  }, [claims]);

  const renderEvidence = (entry: Record<string, unknown>) => {
    const sourceCount = getSourceCount(entry);
    const origin = getOrigin(entry);
    const sourceIds = getSourceIds(entry);
    const memorySourceIds = new Set(getMemorySourceIds(entry));
    return (
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        {sourceIds.length > 0 ? (
          <details className="group max-w-full">
            <summary className="cursor-pointer list-none rounded border border-transparent px-0.5 py-0.5 hover:border-border focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
              {t("sourceCount", { count: sourceCount ?? sourceIds.length })}
            </summary>
            <div className="mt-1.5 flex max-w-full flex-wrap gap-1.5">
              {sourceIds.map((sourceId) => (
                <span
                  key={sourceId}
                  className="flex max-w-full items-center rounded border border-border/70"
                >
                  <span className="max-w-full truncate px-1.5 py-0.5 font-mono" title={sourceId}>
                    {sourceId}
                  </span>
                  {memorySourceIds.has(sourceId) && (
                    <button
                      type="button"
                      onClick={() => setMemoryToDelete(sourceId)}
                      className="border-l border-border/70 p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      aria-label={t("deleteMemory")}
                      title={t("deleteMemory")}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </span>
              ))}
            </div>
          </details>
        ) : (
          <span>
            {sourceCount === null ? t("notAvailable") : t("sourceCount", { count: sourceCount })}
          </span>
        )}
        {origin && <span className="rounded border border-border px-1.5 py-0.5">{origin}</span>}
        {entry.locked === true && (
          <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">
            {t("locked")}
          </span>
        )}
      </div>
    );
  };

  const renderClaim = (claim: PeerClaim) => {
    const entry = claim as Record<string, unknown>;
    const category = getCategory(entry);
    return (
      <div key={claim.id} className="rounded-lg border border-border/70 bg-background/50 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <p className="text-sm text-foreground whitespace-pre-wrap">{getEntryText(entry)}</p>
          <div className="flex shrink-0 items-center gap-1">
            {category && CARD_CATEGORIES.includes(category) && (
              <span className="rounded border border-border px-1.5 py-0.5 text-xs text-muted-foreground">
                {categoryLabels[category]}
              </span>
            )}
            {claim.status === "active" && (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void toggleClaimLocked(claim)}
                  disabled={mutatingClaimId === claim.id}
                  aria-label={claim.locked ? t("unlockClaim") : t("lockClaim")}
                  title={claim.locked ? t("unlockClaim") : t("lockClaim")}
                >
                  {claim.locked ? <Unlock className="h-4 w-4" /> : <Lock className="h-4 w-4" />}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void deleteClaim(claim)}
                  disabled={mutatingClaimId === claim.id}
                  aria-label={t("deleteClaim")}
                  title={t("deleteClaim")}
                  className="text-destructive hover:text-destructive"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {renderEvidence(entry)}
          {claim.confidence != null && (
            <span className="text-xs text-muted-foreground">
              {t("confidence", { value: Math.round(claim.confidence * 100) })}
            </span>
          )}
        </div>
      </div>
    );
  };

  const selectObserver = (nextObserverId: string) => {
    pairRequestRef.current += 1;
    pairKeyRef.current = `${nextObserverId}\u0000${targetId}`;
    directionalInFlightRef.current = null;
    setObserverId(nextObserverId);
    setOperation(null);
    setOperationFeedback(null);
    setCorrectionPlan(null);
    correctionPlanScopeRef.current = null;
  };

  const selectTarget = (nextTargetId: string) => {
    pairRequestRef.current += 1;
    pairKeyRef.current = `${observerId}\u0000${nextTargetId}`;
    directionalInFlightRef.current = null;
    setTargetId(nextTargetId);
    setOperation(null);
    setOperationFeedback(null);
    setCorrectionPlan(null);
    correctionPlanScopeRef.current = null;
  };

  const swapPair = () => {
    if (!observerId || !targetId) return;
    const nextObserverId = targetId;
    const nextTargetId = observerId;
    pairRequestRef.current += 1;
    pairKeyRef.current = `${nextObserverId}\u0000${nextTargetId}`;
    directionalInFlightRef.current = null;
    setObserverId(nextObserverId);
    setTargetId(nextTargetId);
    setOperation(null);
    setOperationFeedback(null);
    setCorrectionPlan(null);
    correctionPlanScopeRef.current = null;
  };

  if (!enabled) {
    return (
      <FeatureNotEnabled
        icon={UsersRound}
        title={t("disabledTitle")}
        description={t("disabledDescription")}
      />
    );
  }

  if (!currentBank) return null;

  const bootstrapRunning =
    bootstrapStatus?.status === "pending" || bootstrapStatus?.status === "processing";
  const bootstrapProgress = bootstrapStatus?.progress;
  const bootstrapPercent =
    bootstrapProgress?.total && bootstrapProgress.total > 0
      ? Math.min(
          100,
          Math.round(((bootstrapProgress.processed ?? 0) / bootstrapProgress.total) * 100)
        )
      : bootstrapStatus?.status === "completed"
        ? 100
        : null;
  const bootstrapResult = asRecord(bootstrapStatus?.result_metadata?.peer_bootstrap);
  const refreshRunning =
    refreshStatus?.status === "pending" || refreshStatus?.status === "processing";
  const latestRefresh = refreshHistory[0];
  const refreshDetail = refreshStatus?.progress?.detail;

  return (
    <div className="space-y-4">
      <header className="flex flex-col gap-4 border-b border-border/70 pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold text-foreground">{t("title")}</h1>
            <button
              type="button"
              onClick={() => void loadPeers()}
              disabled={loadingPeers}
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              aria-label={t("refresh")}
              title={t("refresh")}
            >
              <RefreshCw className={loadingPeers ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            </button>
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{t("description")}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void runBootstrap()}
            disabled={bootstrapRunning || submittingBootstrap}
          >
            {bootstrapRunning || submittingBootstrap ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Sparkles className="mr-2 h-4 w-4" />
            )}
            {bootstrapRunning ? t("bootstrapRunning") : t("runBootstrap")}
          </Button>
          <Button size="sm" onClick={openCreatePeer}>
            <Plus className="mr-2 h-4 w-4" />
            {t("addPeer")}
          </Button>
        </div>
      </header>

      {peerError && (
        <Alert variant="destructive">
          <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{peerError}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadPeers()}
              disabled={loadingPeers}
            >
              {t("retry")}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {loadingPeers && peers.length === 0 ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t("loadingPeers")}
          </CardContent>
        </Card>
      ) : peers.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-14 text-center">
            <UsersRound className="mb-3 h-8 w-8 text-muted-foreground" />
            <h2 className="font-semibold text-foreground">{t("emptyTitle")}</h2>
            <p className="mt-1 max-w-md text-sm text-muted-foreground">{t("emptyDescription")}</p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              <Button size="sm" variant="outline" onClick={() => void runBootstrap()}>
                <Sparkles className="mr-2 h-4 w-4" />
                {t("runBootstrap")}
              </Button>
              <Button size="sm" onClick={openCreatePeer}>
                <Plus className="mr-2 h-4 w-4" />
                {t("addFirstPeer")}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[280px_minmax(0,1fr)] lg:items-start">
          <section
            aria-labelledby="peer-directory-title"
            className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card"
          >
            <div className="border-b border-border/70 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 id="peer-directory-title" className="text-sm font-semibold text-foreground">
                    {t("registryTitle")}
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {t("registryDescription", { count: peers.length })}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={openCreatePeer}
                  aria-label={t("addPeer")}
                >
                  <Plus className="h-4 w-4" />
                </Button>
              </div>

              <div className="mt-4 space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor="peer-observer">{t("observerLabel")}</Label>
                  <Select value={observerId} onValueChange={selectObserver}>
                    <SelectTrigger id="peer-observer">
                      <SelectValue placeholder={t("selectPeer")} />
                    </SelectTrigger>
                    <SelectContent>
                      {peers.map((peer) => (
                        <SelectItem key={peer.id} value={peer.id}>
                          {peer.display_name || peer.external_id}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-1.5 lg:hidden">
                  <Label htmlFor="peer-target-mobile">{t("targetLabel")}</Label>
                  <Select value={targetId} onValueChange={selectTarget}>
                    <SelectTrigger id="peer-target-mobile">
                      <SelectValue placeholder={t("selectPeer")} />
                    </SelectTrigger>
                    <SelectContent>
                      {peers.map((peer) => (
                        <SelectItem key={peer.id} value={peer.id}>
                          {peer.display_name || peer.external_id}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="relative hidden lg:block">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={peerSearch}
                    onChange={(event) => setPeerSearch(event.target.value)}
                    placeholder={t("selectPeer")}
                    aria-label={t("registryTitle")}
                    className="pl-9"
                  />
                </div>
              </div>
            </div>

            <div className="hidden max-h-[calc(100vh-18rem)] overflow-y-auto p-2 lg:block">
              {filteredPeers.map((peer) => {
                const selected = peer.id === targetId;
                return (
                  <div
                    key={peer.id}
                    className={
                      selected
                        ? "group flex items-center rounded-xl bg-primary/10 text-foreground"
                        : "group flex items-center rounded-xl text-foreground hover:bg-muted/70"
                    }
                  >
                    <button
                      type="button"
                      onClick={() => selectTarget(peer.id)}
                      className="min-w-0 flex-1 px-3 py-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                      aria-pressed={selected}
                    >
                      <span className="block truncate text-sm font-medium">
                        {peer.display_name || peer.external_id}
                      </span>
                      <span className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                        <span className="truncate font-mono">{peer.external_id}</span>
                        <span aria-hidden="true">·</span>
                        <span>{kindLabels[peer.kind] ?? peer.kind}</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => openEditPeer(peer)}
                      className="mr-2 rounded-md p-1.5 text-muted-foreground opacity-70 hover:bg-background hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100"
                      aria-label={t("editPeerAria", { id: peer.id })}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })}
              {filteredPeers.length === 0 && (
                <p className="px-3 py-8 text-center text-sm text-muted-foreground">
                  {t("notAvailable")}
                </p>
              )}
            </div>
          </section>

          <section
            aria-labelledby="peer-dossier-title"
            className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card"
          >
            <div className="border-b border-border/70 px-4 py-4 sm:px-5">
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div className="min-w-0">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    {t("directionTitle")}
                  </p>
                  <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
                    <h2
                      id="peer-dossier-title"
                      className="truncate text-xl font-semibold text-foreground"
                    >
                      {observer?.display_name || observer?.external_id || t("selectPeer")}
                    </h2>
                    <ArrowRight
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                      aria-hidden="true"
                    />
                    <span className="truncate text-xl font-semibold text-foreground">
                      {target?.display_name || target?.external_id || t("selectPeer")}
                    </span>
                    {observerId && targetId && observerId === targetId && (
                      <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                        {t("selfPair")}
                      </span>
                    )}
                  </div>
                  {targetId && (
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      {context?.version != null && (
                        <span>{t("version", { version: context.version })}</span>
                      )}
                      {context?.updated_at && (
                        <span>
                          {t("updatedAt", { date: new Date(context.updated_at).toLocaleString() })}
                        </span>
                      )}
                      {loadingContext && (
                        <span className="inline-flex items-center gap-1.5" aria-live="polite">
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          {t("loadingContext")}
                        </span>
                      )}
                    </div>
                  )}
                </div>

                {targetId && (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      onClick={() => void runModelAction("model")}
                      disabled={operation !== null}
                    >
                      {operation === "model" ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <RefreshCw className="mr-2 h-4 w-4" />
                      )}
                      {operation === "model" ? t("modeling") : t("model")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={openCorrection}
                      disabled={operation !== null}
                    >
                      <Pencil className="mr-2 h-4 w-4" />
                      {t("addCorrection")}
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={swapPair}
                      disabled={!observerId || !targetId || operation !== null}
                      aria-label={`${t("targetLabel")} / ${t("observerLabel")}`}
                      title={`${t("targetLabel")} / ${t("observerLabel")}`}
                    >
                      <ArrowLeftRight className="h-4 w-4" />
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button size="icon" variant="ghost" aria-label={t("rebuild")}>
                          <MoreHorizontal className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onClick={() => void runModelAction("rebuild")}
                          disabled={operation !== null}
                        >
                          <RotateCcw className="mr-2 h-4 w-4" />
                          {operation === "rebuild" ? t("rebuilding") : t("rebuild")}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                )}
              </div>

              {operationFeedback && (
                <div
                  className={
                    operationFeedback.status === "failed"
                      ? "mt-4 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
                      : "mt-4 rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm text-muted-foreground"
                  }
                  role="status"
                  aria-live="polite"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    {!isTerminalOperation(operationFeedback.status) && (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    )}
                    <span className="font-medium text-foreground">
                      {t(`bootstrapStatus.${operationFeedback.status}`)}
                    </span>
                    <span className="truncate font-mono text-xs">
                      {operationFeedback.operationId}
                    </span>
                  </div>
                  {operationFeedback.errorMessage && (
                    <p className="mt-1 text-xs">{operationFeedback.errorMessage}</p>
                  )}
                </div>
              )}
            </div>

            {!targetId ? (
              <div className="flex min-h-[420px] flex-col items-center justify-center px-6 py-16 text-center">
                <ArrowRight className="mb-4 h-8 w-8 text-muted-foreground/60" />
                <h3 className="text-base font-semibold text-foreground">{t("targetLabel")}</h3>
                <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                  {t("directionDescription")}
                </p>
              </div>
            ) : contextError ? (
              <div className="p-5">
                <Alert variant="destructive">
                  <AlertTitle>{t("contextErrorTitle")}</AlertTitle>
                  <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                    <span>{contextError}</span>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void loadDirectionalContext()}
                    >
                      {t("retry")}
                    </Button>
                  </AlertDescription>
                </Alert>
              </div>
            ) : notModeled ? (
              <div className="flex min-h-[420px] flex-col items-center justify-center px-6 py-16 text-center">
                <Sparkles className="mb-4 h-8 w-8 text-muted-foreground/60" />
                <h3 className="text-base font-semibold text-foreground">{t("noClaims")}</h3>
                <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                  {t("cardDescription")}
                </p>
                <Button className="mt-5" size="sm" onClick={() => void runModelAction("model")}>
                  <RefreshCw className="mr-2 h-4 w-4" />
                  {t("model")}
                </Button>
              </div>
            ) : loadingContext && context === null ? (
              <div className="flex min-h-[420px] items-center justify-center gap-2 px-6 py-16 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("loadingContext")}
              </div>
            ) : (
              <Tabs defaultValue="card" className="min-w-0">
                <TabsList className="h-auto w-full max-w-full justify-start gap-5 overflow-x-auto rounded-none border-b border-border/70 bg-transparent px-4 py-0 sm:px-5">
                  <TabsTrigger
                    value="card"
                    className="shrink-0 rounded-none border-b-2 border-transparent px-0 py-3 shadow-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                  >
                    {t("cardTitle")}
                  </TabsTrigger>
                  <TabsTrigger
                    value="claims"
                    className="shrink-0 rounded-none border-b-2 border-transparent px-0 py-3 shadow-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                  >
                    {t("claimsTitle")}
                  </TabsTrigger>
                  <TabsTrigger
                    value="activity"
                    className="shrink-0 rounded-none border-b-2 border-transparent px-0 py-3 shadow-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                  >
                    {t("refreshStatusTitle")}
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="card" className="m-0 space-y-6 p-4 sm:p-5">
                  <section aria-labelledby="peer-representation-heading">
                    <h3
                      id="peer-representation-heading"
                      className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground"
                    >
                      {t("representationTitle")}
                    </h3>
                    {(() => {
                      const representation = getNestedValue(context, "representation");
                      if (representation == null) {
                        return (
                          <p className="mt-2 text-sm text-muted-foreground">{t("notAvailable")}</p>
                        );
                      }
                      return (
                        <p className="mt-2 max-w-4xl whitespace-pre-wrap text-sm leading-6 text-foreground">
                          {typeof representation === "string"
                            ? representation
                            : JSON.stringify(representation, null, 2)}
                        </p>
                      );
                    })()}
                  </section>

                  <div className="border-t border-border/70" />

                  {CARD_CATEGORIES.some((category) => card[category].length > 0) ? (
                    <div className="grid gap-x-8 gap-y-6 xl:grid-cols-2">
                      {CARD_CATEGORIES.filter((category) => card[category].length > 0).map(
                        (category) => (
                          <section
                            key={category}
                            aria-labelledby={`peer-card-${category.toLowerCase()}`}
                          >
                            <h3
                              id={`peer-card-${category.toLowerCase()}`}
                              className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground"
                            >
                              {categoryLabels[category]}
                            </h3>
                            <div className="mt-3 divide-y divide-border/60">
                              {card[category].map((entry, index) => {
                                const record = entry as Record<string, unknown>;
                                const claimId = getClaimId(record);
                                const matchingClaim = claimId ? claimsById.get(claimId) : undefined;
                                const evidenceRecord = (matchingClaim ??
                                  entry) as unknown as Record<string, unknown>;
                                return (
                                  <article
                                    key={claimId ?? String(index)}
                                    className="py-3 first:pt-0 last:pb-0"
                                  >
                                    <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">
                                      {getEntryText(record)}
                                    </p>
                                    <div className="mt-2">{renderEvidence(evidenceRecord)}</div>
                                  </article>
                                );
                              })}
                            </div>
                          </section>
                        )
                      )}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">{t("noClaims")}</p>
                  )}
                </TabsContent>

                <TabsContent value="claims" className="m-0 space-y-5 p-4 sm:p-5">
                  {claimsError && (
                    <Alert variant="destructive">
                      <AlertTitle>{t("claimsErrorTitle")}</AlertTitle>
                      <AlertDescription>{claimsError}</AlertDescription>
                    </Alert>
                  )}
                  {claims.length === 0 ? (
                    <p className="text-sm text-muted-foreground">{t("noClaims")}</p>
                  ) : (
                    [
                      ...CLAIM_STATUSES,
                      ...Object.keys(claimGroups).filter(
                        (status) => !CLAIM_STATUSES.includes(status as ClaimStatusGroup)
                      ),
                    ]
                      .filter((status) => (claimGroups[status] ?? []).length > 0)
                      .map((status) => (
                        <section key={status} aria-labelledby={`peer-claims-${status}`}>
                          <h3
                            id={`peer-claims-${status}`}
                            className="mb-2 text-sm font-semibold text-foreground"
                          >
                            {statusLabels[status as ClaimStatusGroup] ?? status}
                            <span className="ml-2 text-xs font-normal text-muted-foreground">
                              {claimGroups[status]?.length ?? 0}
                            </span>
                          </h3>
                          <div className="space-y-2">
                            {(claimGroups[status] ?? []).map(renderClaim)}
                          </div>
                        </section>
                      ))
                  )}
                </TabsContent>

                <TabsContent value="activity" className="m-0 space-y-6 p-4 sm:p-5">
                  <section aria-labelledby="peer-refresh-heading" className="space-y-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h3
                          id="peer-refresh-heading"
                          className="text-sm font-semibold text-foreground"
                        >
                          {t("refreshStatusTitle")}
                        </h3>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {t("refreshStatusDescription")}
                        </p>
                      </div>
                      <span className="rounded-full border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground">
                        {t(`bootstrapStatus.${refreshStatus?.status ?? "notStarted"}`)}
                      </span>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="rounded-xl bg-muted/35 px-3 py-2.5">
                        <p className="text-xs text-muted-foreground">{t("refreshStageLabel")}</p>
                        <p className="mt-1 text-sm font-medium">
                          {refreshStatus?.progress?.stage ?? t("notAvailable")}
                        </p>
                      </div>
                      <div className="rounded-xl bg-muted/35 px-3 py-2.5">
                        <p className="text-xs text-muted-foreground">{t("lastAttemptLabel")}</p>
                        <p className="mt-1 text-sm font-medium">
                          {latestRefresh
                            ? new Date(latestRefresh.created_at).toLocaleString()
                            : t("notAvailable")}
                        </p>
                      </div>
                      <div className="rounded-xl bg-muted/35 px-3 py-2.5">
                        <p className="text-xs text-muted-foreground">{t("nextEligibleLabel")}</p>
                        <p className="mt-1 text-sm font-medium">
                          {peerCooldownSeconds > 0 ? `${peerCooldownSeconds}s` : t("notAvailable")}
                        </p>
                      </div>
                    </div>
                    {refreshStatus?.progress && (
                      <div
                        className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
                        aria-live="polite"
                      >
                        {refreshStatus.progress.processed != null &&
                          refreshStatus.progress.total != null && (
                            <span>
                              {t("refreshPairsProcessed", {
                                processed: refreshStatus.progress.processed,
                                total: refreshStatus.progress.total,
                              })}
                            </span>
                          )}
                        {typeof refreshDetail?.refreshed === "number" &&
                          typeof refreshDetail?.unchanged === "number" &&
                          typeof refreshDetail?.failed === "number" && (
                            <span>
                              {t("refreshOutcomeSummary", {
                                refreshed: refreshDetail.refreshed,
                                unchanged: refreshDetail.unchanged,
                                failed: refreshDetail.failed,
                              })}
                            </span>
                          )}
                        <span>
                          {t("refreshRetryCount", { count: refreshStatus.retry_count ?? 0 })}
                        </span>
                      </div>
                    )}
                    {refreshRunning && (
                      <p className="text-xs text-muted-foreground">{t("refreshStillMutating")}</p>
                    )}
                    {(refreshError || refreshStatus?.status === "failed") && (
                      <Alert variant="destructive">
                        <AlertTitle>{t("refreshFailedTitle")}</AlertTitle>
                        <AlertDescription>
                          {refreshError ??
                            refreshStatus?.error_message ??
                            t("refreshFailedDescription")}
                        </AlertDescription>
                      </Alert>
                    )}
                    {refreshHistory.length > 0 && (
                      <details className="rounded-xl border border-border/70 px-3 py-2.5">
                        <summary className="cursor-pointer text-sm font-medium text-foreground">
                          {t("refreshStatusTitle")}
                        </summary>
                        <div className="mt-3 space-y-2">
                          {refreshHistory.map((item) => (
                            <div
                              key={item.id}
                              className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center sm:gap-3"
                            >
                              <span className="truncate font-mono" title={item.id}>
                                {item.id}
                              </span>
                              <span>{t(`bootstrapStatus.${item.status}`)}</span>
                              <span>{new Date(item.created_at).toLocaleString()}</span>
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                  </section>

                  <section
                    aria-labelledby="peer-bootstrap-heading"
                    className="space-y-3 border-t border-border/70 pt-5"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h3
                          id="peer-bootstrap-heading"
                          className="text-sm font-semibold text-foreground"
                        >
                          {t("bootstrapTitle")}
                        </h3>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {t("bootstrapDescription")}
                        </p>
                      </div>
                      <span className="rounded-full border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground">
                        {t(`bootstrapStatus.${bootstrapStatus?.status ?? "notStarted"}`)}
                      </span>
                    </div>
                    {(bootstrapRunning || bootstrapPercent !== null) && (
                      <div
                        role="progressbar"
                        aria-label={t("bootstrapTitle")}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={bootstrapPercent ?? undefined}
                        aria-busy={bootstrapRunning}
                        className="h-2 overflow-hidden rounded-full bg-muted"
                      >
                        <div
                          className={
                            bootstrapPercent === null
                              ? "h-full w-1/3 animate-pulse rounded-full bg-primary"
                              : "h-full rounded-full bg-primary transition-[width] duration-300"
                          }
                          style={
                            bootstrapPercent === null
                              ? undefined
                              : { width: `${bootstrapPercent}%` }
                          }
                        />
                      </div>
                    )}
                    <div
                      className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"
                      aria-live="polite"
                    >
                      <span>
                        {bootstrapProgress
                          ? t("bootstrapStage", { stage: bootstrapProgress.stage })
                          : t("bootstrapWaiting")}
                      </span>
                      {bootstrapPercent !== null && <span>{bootstrapPercent}%</span>}
                    </div>
                    {bootstrapResult && (
                      <p className="text-xs text-muted-foreground">
                        {t("bootstrapSummary", {
                          evidence: Number(bootstrapResult.evidence_processed ?? 0),
                          peers: Number(bootstrapResult.peers_discovered ?? 0),
                          claims: Number(bootstrapResult.claims_materialized ?? 0),
                          cards: Number(bootstrapResult.card_entries ?? 0),
                        })}
                      </p>
                    )}
                    {(bootstrapError || bootstrapStatus?.error_message) && (
                      <Alert variant="destructive">
                        <AlertTitle>{t("bootstrapErrorTitle")}</AlertTitle>
                        <AlertDescription>
                          {bootstrapError ?? bootstrapStatus?.error_message}
                        </AlertDescription>
                      </Alert>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void runBootstrap()}
                      disabled={bootstrapRunning || submittingBootstrap}
                    >
                      {bootstrapRunning || submittingBootstrap ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <Sparkles className="mr-2 h-4 w-4" />
                      )}
                      {bootstrapRunning ? t("bootstrapRunning") : t("runBootstrap")}
                    </Button>
                  </section>
                </TabsContent>
              </Tabs>
            )}
          </section>
        </div>
      )}

      <Dialog open={peerDialogOpen} onOpenChange={setPeerDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingPeer ? t("editPeerTitle") : t("createPeerTitle")}</DialogTitle>
            <DialogDescription>
              {editingPeer ? t("editPeerDescription") : t("createPeerDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="peer-id">{t("peerIdLabel")}</Label>
              <Input
                id="peer-id"
                value={peerForm.id}
                disabled={editingPeer !== null}
                onChange={(event) =>
                  setPeerForm((current) => ({ ...current, id: event.target.value }))
                }
                placeholder={t("peerIdPlaceholder")}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="peer-display-name">{t("displayNameLabel")}</Label>
              <Input
                id="peer-display-name"
                value={peerForm.displayName}
                onChange={(event) =>
                  setPeerForm((current) => ({ ...current, displayName: event.target.value }))
                }
                placeholder={t("displayNamePlaceholder")}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="peer-kind">{t("kindLabel")}</Label>
              <Select
                value={peerForm.kind}
                onValueChange={(kind) => setPeerForm((current) => ({ ...current, kind }))}
              >
                <SelectTrigger id="peer-kind">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PEER_KINDS.map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {kindLabels[kind]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="peer-metadata">{t("metadataLabel")}</Label>
              <Textarea
                id="peer-metadata"
                value={peerForm.metadata}
                onChange={(event) =>
                  setPeerForm((current) => ({ ...current, metadata: event.target.value }))
                }
                placeholder={t("metadataPlaceholder")}
                rows={6}
              />
              <p className="text-xs text-muted-foreground">{t("metadataHelp")}</p>
            </div>
            {peerFormError && (
              <Alert variant="destructive">
                <AlertDescription>{peerFormError}</AlertDescription>
              </Alert>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPeerDialogOpen(false)}
              disabled={savingPeer}
            >
              {t("cancel")}
            </Button>
            <Button onClick={() => void savePeer()} disabled={savingPeer || !peerForm.id.trim()}>
              {savingPeer && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {editingPeer ? t("savePeer") : t("createPeer")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={memoryToDelete !== null}
        onOpenChange={(open) => {
          if (!open && deletingMemoryId === null) setMemoryToDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("deleteMemory")}</DialogTitle>
            <DialogDescription>{t("deleteMemoryConfirm")}</DialogDescription>
          </DialogHeader>
          {memoryToDelete && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 font-mono text-xs text-foreground">
              {memoryToDelete}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setMemoryToDelete(null)}
              disabled={deletingMemoryId !== null}
            >
              {t("cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => void deleteMemorySource()}
              disabled={deletingMemoryId !== null}
            >
              {deletingMemoryId !== null && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("deleteMemory")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={correctionDialogOpen} onOpenChange={setCorrectionDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("correctionTitle")}</DialogTitle>
            <DialogDescription>{t("correctionDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="correction-text">{t("correctionTextLabel")}</Label>
              <Textarea
                id="correction-text"
                value={correctionText}
                onChange={(event) => {
                  setCorrectionText(event.target.value);
                  setCorrectionPlan(null);
                }}
                placeholder={t("correctionTextPlaceholder")}
                rows={6}
              />
            </div>
            {correctionPlan ? (
              <div className="space-y-4 rounded-lg border bg-muted/30 p-4">
                <div className="space-y-2">
                  <Label>{t("correctionPlanAdds")}</Label>
                  <div className="space-y-2">
                    {correctionPlan.claims.map((claim, index) => (
                      <div
                        key={`${claim.claim_type}-${index}`}
                        className="rounded-md border bg-background p-3 text-sm"
                      >
                        <div className="mb-1 text-xs font-medium text-muted-foreground">
                          {categoryLabels[claim.claim_type as CardCategory] ?? claim.claim_type}
                        </div>
                        {claim.text}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>{t("correctionPlanReplaces")}</Label>
                  {correctionPlan.supersede_claim_ids.length > 0 ? (
                    <div className="space-y-2">
                      {correctionPlan.supersede_claim_ids.map((claimId) => {
                        const claim = claims.find((candidate) => candidate.id === claimId);
                        return (
                          <div
                            key={claimId}
                            className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
                          >
                            {claim
                              ? getEntryText(claim as unknown as Record<string, unknown>)
                              : claimId}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {t("correctionPlanNoReplacements")}
                    </p>
                  )}
                </div>
                <Alert>
                  <AlertDescription>{correctionPlan.reason}</AlertDescription>
                </Alert>
              </div>
            ) : (
              <Alert>
                <AlertDescription>{t("correctionLockedNotice")}</AlertDescription>
              </Alert>
            )}
            {correctionError && (
              <Alert variant="destructive">
                <AlertDescription>{correctionError}</AlertDescription>
              </Alert>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCorrectionDialogOpen(false)}
              disabled={savingCorrection}
            >
              {t("cancel")}
            </Button>
            {correctionPlan ? (
              <>
                <Button
                  variant="outline"
                  onClick={() => setCorrectionPlan(null)}
                  disabled={savingCorrection}
                >
                  {t("editCorrection")}
                </Button>
                <Button onClick={() => void submitCorrection()} disabled={savingCorrection}>
                  {savingCorrection && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {t("applyCorrection")}
                </Button>
              </>
            ) : (
              <Button
                onClick={() => void reviewCorrection()}
                disabled={savingCorrection || !correctionText.trim()}
              >
                {savingCorrection && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {t("reviewCorrection")}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
