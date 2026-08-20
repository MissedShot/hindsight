"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";
import {
  ArrowRight,
  Check,
  Lock,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
import { FeatureNotEnabled } from "@/components/feature-not-enabled";

const PEER_KINDS = ["person", "agent", "team", "project", "organization", "other"] as const;
const CARD_CATEGORIES = ["IDENTITY", "ATTRIBUTE", "RELATIONSHIP", "INSTRUCTION"] as const;
type CardCategory = (typeof CARD_CATEGORIES)[number];
const CLAIM_STATUSES = ["active", "contested", "superseded", "retracted"] as const;
type ClaimStatusGroup = (typeof CLAIM_STATUSES)[number];

type PeerForm = {
  id: string;
  kind: PeerKind;
  metadata: string;
};

type OperationFeedback = {
  operationId: string;
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

const EMPTY_PEER_FORM: PeerForm = { id: "", kind: "person", metadata: "" };

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

function getSourceCount(entry: Record<string, unknown>): number {
  for (const key of ["source_count", "evidence_count"]) {
    const value = entry[key];
    if (typeof value === "number") return value;
  }
  if (Array.isArray(entry.source_ids)) return entry.source_ids.length;
  if (Array.isArray(entry.sources)) return entry.sources.length;
  if (Array.isArray(entry.evidence)) return entry.evidence.length;
  return 0;
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
  const [context, setContext] = useState<PeerContextResponse | null>(null);
  const [claims, setClaims] = useState<PeerClaim[]>([]);
  const [loadingContext, setLoadingContext] = useState(false);
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

  // Hard reset of every projection when the bank scope changes (or peer
  // modeling is disabled). All async work from the previous scope is
  // invalidated by bumping the generation before the new scope's loaders run.
  useEffect(() => {
    scopeGenerationRef.current += 1;
    bootstrapPollingRef.current = false;
    refreshPollingRef.current = false;
    expectedBootstrapOperationRef.current = null;
    setPeers([]);
    setLoadingPeers(false);
    setPeerError(null);
    setObserverId("");
    setTargetId("");
    setContext(null);
    setClaims([]);
    setLoadingContext(false);
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
    };
  }, []);

  const observer = useMemo(() => peers.find((peer) => peer.id === observerId), [peers, observerId]);
  const target = useMemo(() => peers.find((peer) => peer.id === targetId), [peers, targetId]);
  const card = useMemo(() => extractCard(context), [context]);

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
      const nextPeers = normalizePeers(await client.listPeers(currentBank));
      if (!isCurrentScope(generation)) return;
      setPeers(nextPeers);
      setObserverId((current) =>
        nextPeers.some((peer) => peer.id === current) ? current : (nextPeers[0]?.id ?? "")
      );
      setTargetId((current) =>
        nextPeers.some((peer) => peer.id === current) ? current : (nextPeers[0]?.id ?? "")
      );
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
      setRefreshStatus(status);
      setRefreshError(null);
    } catch (error) {
      if (isCurrentScope(generation)) setRefreshError(getErrorMessage(error));
    } finally {
      if (isCurrentScope(generation)) refreshPollingRef.current = false;
    }
  }, [currentBank, enabled, isCurrentScope]);

  const loadDirectionalContext = useCallback(async () => {
    if (!currentBank || !enabled || !observerId || !targetId) return;
    const generation = scopeGenerationRef.current;
    setLoadingContext(true);
    setContextError(null);
    setContext(null);
    setClaims([]);
    setClaimsError(null);
    const [contextResult, claimsResult] = await Promise.allSettled([
      client.getPeerContext(currentBank, observerId, targetId),
      client.getPeerClaims(currentBank, observerId, targetId),
    ]);
    if (!isCurrentScope(generation)) return;
    if (contextResult.status === "fulfilled") setContext(contextResult.value);
    if (claimsResult.status === "fulfilled") setClaims(normalizeClaims(claimsResult.value));
    if (contextResult.status === "rejected") setContextError(getErrorMessage(contextResult.reason));
    if (claimsResult.status === "rejected") setClaimsError(getErrorMessage(claimsResult.reason));
    setLoadingContext(false);
  }, [currentBank, enabled, observerId, targetId, isCurrentScope]);

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

  const openCreatePeer = () => {
    setEditingPeer(null);
    setPeerForm(EMPTY_PEER_FORM);
    setPeerFormError(null);
    setPeerDialogOpen(true);
  };

  const openEditPeer = (peer: Peer) => {
    setEditingPeer(peer);
    setPeerForm({ id: peer.external_id, kind: peer.kind, metadata: formatMetadata(peer.metadata) });
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
          kind: peerForm.kind,
          ...(metadata === undefined ? {} : { metadata }),
        });
        if (!isCurrentScope(generation)) return;
        toast.success(t("peerUpdated"));
      } else {
        await client.createPeer(currentBank, {
          external_id: peerForm.id.trim(),
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
    setOperation(kind);
    setOperationFeedback(null);
    try {
      if (kind === "model") {
        const result = await client.modelPeer(currentBank, observerId, targetId);
        if (!isCurrentScope(generation)) return;
        setOperationFeedback({ operationId: result.operation_id });
        toast.success(t("operationSubmitted"), {
          description: t("operationSubmittedWithId", { id: result.operation_id }),
        });
      } else {
        await client.rebuildPeer(currentBank, observerId, targetId);
        if (!isCurrentScope(generation)) return;
        await loadDirectionalContext();
        if (!isCurrentScope(generation)) return;
        toast.success(t("rebuildOperationFeedback"));
      }
    } catch {
      // The API client displays the structured error toast.
    } finally {
      if (isCurrentScope(generation)) setOperation(null);
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

  const deleteMemorySource = async (memoryId: string) => {
    if (!currentBank || !window.confirm(t("deleteMemoryConfirm"))) return;
    const generation = scopeGenerationRef.current;
    setDeletingMemoryId(memoryId);
    try {
      await client.deleteMemory(memoryId, currentBank);
      if (!isCurrentScope(generation)) return;
      toast.success(t("memoryDeleted"));
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
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        <span>{t("sourceCount", { count: sourceCount })}</span>
        {origin && <span className="rounded border border-border px-1.5 py-0.5">{origin}</span>}
        {sourceIds.slice(0, 5).map((sourceId) =>
          memorySourceIds.has(sourceId) ? (
            <button
              key={sourceId}
              type="button"
              onClick={() => void deleteMemorySource(sourceId)}
              disabled={deletingMemoryId === sourceId}
              className="inline-flex items-center gap-1 rounded border border-border/70 px-1.5 py-0.5 font-mono hover:border-destructive/50 hover:text-destructive disabled:opacity-50"
              title={t("deleteMemory")}
            >
              {sourceId}
              <Trash2 className="h-3 w-3" />
            </button>
          ) : (
            <span
              key={sourceId}
              className="rounded border border-border/70 px-1.5 py-0.5 font-mono"
            >
              {sourceId}
            </span>
          )
        )}
        {sourceIds.length > 5 && <span>{t("moreSources", { count: sourceIds.length - 5 })}</span>}
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
    bootstrapStatus?.status === "completed"
      ? 100
      : bootstrapProgress?.total && bootstrapProgress.total > 0
        ? Math.min(
            100,
            Math.round(((bootstrapProgress.processed ?? 0) / bootstrapProgress.total) * 100)
          )
        : 0;
  const bootstrapResult = asRecord(bootstrapStatus?.result_metadata?.peer_bootstrap);
  const refreshRunning =
    refreshStatus?.status === "pending" || refreshStatus?.status === "processing";
  const latestRefresh = refreshHistory[0];
  const refreshDetail = refreshStatus?.progress?.detail;
  const nextEligibleAt = latestRefresh
    ? new Date(
        new Date(latestRefresh.updated_at ?? latestRefresh.created_at).getTime() +
          peerCooldownSeconds * 1000
      )
    : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold text-foreground">{t("title")}</h1>
            <button
              type="button"
              onClick={() => void loadPeers()}
              disabled={loadingPeers}
              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
              aria-label={t("refresh")}
              title={t("refresh")}
            >
              <RefreshCw className={loadingPeers ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            </button>
          </div>
          <p className="mt-2 text-muted-foreground">{t("description")}</p>
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
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-lg">{t("refreshStatusTitle")}</CardTitle>
              <CardDescription>{t("refreshStatusDescription")}</CardDescription>
            </div>
            <span className="rounded-full border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              {t(`bootstrapStatus.${refreshStatus?.status ?? "notStarted"}`)}
            </span>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-lg border border-border/70 px-3 py-2">
              <p className="text-xs text-muted-foreground">{t("refreshStageLabel")}</p>
              <p className="mt-1 text-sm font-medium">
                {refreshStatus?.progress?.stage ?? t("notAvailable")}
              </p>
            </div>
            <div className="rounded-lg border border-border/70 px-3 py-2">
              <p className="text-xs text-muted-foreground">{t("lastAttemptLabel")}</p>
              <p className="mt-1 text-sm font-medium">
                {latestRefresh
                  ? new Date(latestRefresh.created_at).toLocaleString()
                  : t("notAvailable")}
              </p>
            </div>
            <div className="rounded-lg border border-border/70 px-3 py-2">
              <p className="text-xs text-muted-foreground">{t("nextEligibleLabel")}</p>
              <p className="mt-1 text-sm font-medium">
                {nextEligibleAt ? nextEligibleAt.toLocaleString() : t("notAvailable")}
              </p>
            </div>
          </div>
          {refreshRunning && (
            <p className="text-xs text-muted-foreground">{t("refreshStillMutating")}</p>
          )}
          {refreshStatus?.progress && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {refreshStatus.progress.processed != null && refreshStatus.progress.total != null && (
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
              <span>{t("refreshRetryCount", { count: refreshStatus.retry_count ?? 0 })}</span>
              {refreshStatus.next_retry_at && (
                <span>
                  {t("refreshNextRetry", {
                    date: new Date(refreshStatus.next_retry_at).toLocaleString(),
                  })}
                </span>
              )}
            </div>
          )}
          {refreshStatus?.status === "failed" && (
            <Alert variant="destructive">
              <AlertTitle>{t("refreshFailedTitle")}</AlertTitle>
              <AlertDescription>{t("refreshFailedDescription")}</AlertDescription>
            </Alert>
          )}
          {refreshError && (
            <Alert variant="destructive">
              <AlertTitle>{t("refreshErrorTitle")}</AlertTitle>
              <AlertDescription>{refreshError}</AlertDescription>
            </Alert>
          )}
          {refreshHistory.length > 0 && (
            <div className="space-y-1">
              {refreshHistory.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center justify-between gap-3 text-xs text-muted-foreground"
                >
                  <span className="truncate font-mono">{item.id}</span>
                  <span>{t(`bootstrapStatus.${item.status}`)}</span>
                  <span className="shrink-0">{new Date(item.created_at).toLocaleString()}</span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-lg">{t("bootstrapTitle")}</CardTitle>
              <CardDescription>{t("bootstrapDescription")}</CardDescription>
            </div>
            <span className="rounded-full border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              {t(`bootstrapStatus.${bootstrapStatus?.status ?? "notStarted"}`)}
            </span>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full bg-primary transition-[width] duration-300"
              style={{ width: `${bootstrapPercent}%` }}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {bootstrapProgress
                ? t("bootstrapStage", { stage: bootstrapProgress.stage })
                : t("bootstrapWaiting")}
            </span>
            <span>{bootstrapPercent}%</span>
          </div>
          {bootstrapProgress?.detail && (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {[
                "evidence_processed",
                "peers_discovered",
                "claims_materialized",
                "card_entries",
              ].map((key) => (
                <div key={key} className="rounded-lg border border-border/70 px-3 py-2">
                  <p className="text-xs text-muted-foreground">{t(`bootstrapCounter.${key}`)}</p>
                  <p className="mt-1 font-mono text-sm text-foreground">
                    {bootstrapProgress.detail?.[key] ?? 0}
                  </p>
                </div>
              ))}
            </div>
          )}
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
          {bootstrapStatus?.operation_id && (
            <p className="break-all font-mono text-[11px] text-muted-foreground">
              {bootstrapStatus.operation_id}
            </p>
          )}
        </CardContent>
      </Card>

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

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">{t("registryTitle")}</CardTitle>
          <CardDescription>{t("registryDescription", { count: peers.length })}</CardDescription>
        </CardHeader>
        <CardContent>
          {loadingPeers ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("loadingPeers")}
            </div>
          ) : peers.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border py-10 text-center">
              <UsersRound className="mb-3 h-8 w-8 text-muted-foreground" />
              <p className="font-medium text-foreground">{t("emptyTitle")}</p>
              <p className="mt-1 max-w-md text-sm text-muted-foreground">{t("emptyDescription")}</p>
              <Button className="mt-4" size="sm" onClick={openCreatePeer}>
                <Plus className="mr-2 h-4 w-4" />
                {t("addFirstPeer")}
              </Button>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {peers.map((peer) => (
                <div key={peer.id} className="rounded-lg border border-border/70 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium text-foreground" title={peer.external_id}>
                        {peer.display_name || peer.external_id}
                      </p>
                      {peer.display_name && (
                        <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">
                          {peer.external_id}
                        </p>
                      )}
                      <p className="mt-1 text-xs text-muted-foreground">
                        {kindLabels[peer.kind] ?? peer.kind}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openEditPeer(peer)}
                      aria-label={t("editPeerAria", { id: peer.id })}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                  </div>
                  {peer.metadata && Object.keys(peer.metadata).length > 0 && (
                    <p className="mt-3 text-xs text-muted-foreground">
                      {t("metadataFields", { count: Object.keys(peer.metadata).length })}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {peers.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">{t("directionTitle")}</CardTitle>
            <CardDescription>{t("directionDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr] md:items-end">
              <div className="space-y-2">
                <Label htmlFor="peer-observer">{t("observerLabel")}</Label>
                <Select value={observerId} onValueChange={setObserverId}>
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
              <ArrowRight
                className="hidden h-5 w-5 text-muted-foreground md:block"
                aria-hidden="true"
              />
              <div className="space-y-2">
                <Label htmlFor="peer-target">{t("targetLabel")}</Label>
                <Select value={targetId} onValueChange={setTargetId}>
                  <SelectTrigger id="peer-target">
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
            </div>
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border/70 bg-muted/20 px-3 py-2 text-sm">
              <span className="font-medium text-foreground">
                {observer?.display_name || observer?.external_id}
              </span>
              <ArrowRight className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              <span className="font-medium text-foreground">
                {target?.display_name || target?.external_id}
              </span>
              {observerId === targetId && (
                <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                  {t("selfPair")}
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                onClick={() => void runModelAction("model")}
                disabled={loadingContext || operation !== null}
              >
                {operation === "model" ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Sparkles className="mr-2 h-4 w-4" />
                )}
                {operation === "model" ? t("modeling") : t("model")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void runModelAction("rebuild")}
                disabled={loadingContext || operation !== null}
              >
                {operation === "rebuild" ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RotateCcw className="mr-2 h-4 w-4" />
                )}
                {operation === "rebuild" ? t("rebuilding") : t("rebuild")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={openCorrection}
                disabled={loadingContext || operation !== null}
              >
                <Pencil className="mr-2 h-4 w-4" />
                {t("addCorrection")}
              </Button>
            </div>
            {operationFeedback && (
              <Alert>
                <Check className="h-4 w-4" />
                <AlertTitle>{t("operationFeedbackTitle")}</AlertTitle>
                <AlertDescription>
                  {t("modelOperationFeedback")}
                  <span className="ml-1 font-mono text-xs">{operationFeedback.operationId}</span>
                </AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      )}

      {peers.length > 0 && (
        <div className="space-y-6">
          {contextError && (
            <Alert variant="destructive">
              <AlertTitle>{t("contextErrorTitle")}</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>{contextError}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadDirectionalContext()}
                  disabled={loadingContext}
                >
                  {t("retry")}
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {loadingContext ? (
            <Card>
              <CardContent className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("loadingContext")}
              </CardContent>
            </Card>
          ) : (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="text-lg">{t("cardTitle")}</CardTitle>
                  <CardDescription>{t("cardDescription")}</CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4 md:grid-cols-2">
                  {CARD_CATEGORIES.map((category) => (
                    <section key={category} className="rounded-lg border border-border/70 p-4">
                      <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                        {categoryLabels[category]}
                      </h3>
                      {card[category].length === 0 ? (
                        <p className="mt-3 text-sm text-muted-foreground">
                          {t("noCategoryClaims")}
                        </p>
                      ) : (
                        <div className="mt-3 space-y-3">
                          {card[category].map((entry, index) => {
                            const record = entry as Record<string, unknown>;
                            return (
                              <div
                                key={String(entry.id ?? index)}
                                className="border-l-2 border-primary/40 pl-3"
                              >
                                <p className="text-sm text-foreground whitespace-pre-wrap">
                                  {getEntryText(record)}
                                </p>
                                <div className="mt-2">{renderEvidence(record)}</div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </section>
                  ))}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-lg">{t("representationTitle")}</CardTitle>
                  <CardDescription>{t("representationDescription")}</CardDescription>
                </CardHeader>
                <CardContent>
                  {(() => {
                    const representation = getNestedValue(context, "representation");
                    if (representation == null)
                      return <p className="text-sm text-muted-foreground">{t("notAvailable")}</p>;
                    return (
                      <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-border/70 bg-muted/20 p-4 font-mono text-sm text-foreground">
                        {typeof representation === "string"
                          ? representation
                          : JSON.stringify(representation, null, 2)}
                      </pre>
                    );
                  })()}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-lg">{t("claimsTitle")}</CardTitle>
                  <CardDescription>{t("claimsDescription")}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-5">
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
                    ].map((status) => {
                      const statusClaims = claimGroups[status] ?? [];
                      return (
                        <section key={status}>
                          <h3 className="mb-2 text-sm font-semibold text-foreground">
                            {statusLabels[status as ClaimStatusGroup] ?? status}
                          </h3>
                          {statusClaims.length === 0 ? (
                            <p className="text-sm text-muted-foreground">{t("noClaimsInStatus")}</p>
                          ) : (
                            <div className="space-y-2">{statusClaims.map(renderClaim)}</div>
                          )}
                        </section>
                      );
                    })
                  )}
                </CardContent>
              </Card>

              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                {context?.version != null && (
                  <span>{t("version", { version: context.version })}</span>
                )}
                {context?.updated_at && (
                  <span>
                    {t("updatedAt", { date: new Date(context.updated_at).toLocaleString() })}
                  </span>
                )}
                <span>{t("directionalNote")}</span>
              </div>
            </>
          )}
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
