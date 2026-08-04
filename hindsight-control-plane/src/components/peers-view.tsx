"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";
import {
  ArrowRight,
  Check,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Sparkles,
  UsersRound,
} from "lucide-react";
import { client, Peer, PeerCardEntry, PeerClaim, PeerContextResponse, PeerKind } from "@/lib/api";
import { useBank } from "@/lib/bank-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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
  kind: "model" | "rebuild";
  operationId: string | null;
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

  const [peerDialogOpen, setPeerDialogOpen] = useState(false);
  const [editingPeer, setEditingPeer] = useState<Peer | null>(null);
  const [peerForm, setPeerForm] = useState<PeerForm>(EMPTY_PEER_FORM);
  const [peerFormError, setPeerFormError] = useState<string | null>(null);
  const [savingPeer, setSavingPeer] = useState(false);

  const [correctionDialogOpen, setCorrectionDialogOpen] = useState(false);
  const [correctionText, setCorrectionText] = useState("");
  const [correctionCategory, setCorrectionCategory] = useState<CardCategory>("ATTRIBUTE");
  const [correctionError, setCorrectionError] = useState<string | null>(null);
  const [savingCorrection, setSavingCorrection] = useState(false);

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
    setLoadingPeers(true);
    setPeerError(null);
    try {
      const nextPeers = normalizePeers(await client.listPeers(currentBank));
      setPeers(nextPeers);
      setObserverId((current) => (nextPeers.some((peer) => peer.id === current) ? current : nextPeers[0]?.id ?? ""));
      setTargetId((current) => (nextPeers.some((peer) => peer.id === current) ? current : nextPeers[0]?.id ?? ""));
    } catch (error) {
      setPeerError(getErrorMessage(error));
    } finally {
      setLoadingPeers(false);
    }
  }, [currentBank, enabled]);

  const loadDirectionalContext = useCallback(async () => {
    if (!currentBank || !enabled || !observerId || !targetId) return;
    setLoadingContext(true);
    setContextError(null);
    setContext(null);
    setClaims([]);
    setClaimsError(null);
    const [contextResult, claimsResult] = await Promise.allSettled([
      client.getPeerContext(currentBank, observerId, targetId),
      client.getPeerClaims(currentBank, observerId, targetId),
    ]);
    if (contextResult.status === "fulfilled") setContext(contextResult.value);
    if (claimsResult.status === "fulfilled") setClaims(normalizeClaims(claimsResult.value));
    if (contextResult.status === "rejected") setContextError(getErrorMessage(contextResult.reason));
    if (claimsResult.status === "rejected") setClaimsError(getErrorMessage(claimsResult.reason));
    setLoadingContext(false);
  }, [currentBank, enabled, observerId, targetId]);

  useEffect(() => {
    void loadPeers();
  }, [loadPeers]);

  useEffect(() => {
    void loadDirectionalContext();
  }, [loadDirectionalContext]);

  const openCreatePeer = () => {
    setEditingPeer(null);
    setPeerForm(EMPTY_PEER_FORM);
    setPeerFormError(null);
    setPeerDialogOpen(true);
  };

  const openEditPeer = (peer: Peer) => {
    setEditingPeer(peer);
    setPeerForm({ id: peer.id, kind: peer.kind, metadata: formatMetadata(peer.metadata) });
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
    setSavingPeer(true);
    setPeerFormError(null);
    try {
      const metadata = parseMetadata();
      if (editingPeer) {
        await client.updatePeer(currentBank, editingPeer.id, {
          kind: peerForm.kind,
          ...(metadata === undefined ? {} : { metadata }),
        });
        toast.success(t("peerUpdated"));
      } else {
        await client.createPeer(currentBank, {
          external_id: peerForm.id.trim(),
          kind: peerForm.kind,
          ...(metadata === undefined ? {} : { metadata }),
        });
        toast.success(t("peerCreated"));
      }
      setPeerDialogOpen(false);
      await loadPeers();
    } catch (error) {
      setPeerFormError(getErrorMessage(error));
    } finally {
      setSavingPeer(false);
    }
  };

  const runModelAction = async (kind: "model" | "rebuild") => {
    if (!currentBank || !observerId || !targetId) return;
    setOperation(kind);
    setOperationFeedback(null);
    try {
      const result = kind === "model"
        ? await client.modelPeer(currentBank, observerId, targetId)
        : await client.rebuildPeer(currentBank, observerId, targetId);
      const operationId = typeof result.operation_id === "string" ? result.operation_id : null;
      setOperationFeedback({ kind, operationId });
      toast.success(t("operationSubmitted"), {
        description: operationId ? t("operationSubmittedWithId", { id: operationId }) : t("operationSubmittedWithoutId"),
      });
    } catch {
      // The API client displays the structured error toast.
    } finally {
      setOperation(null);
    }
  };

  const openCorrection = () => {
    setCorrectionText("");
    setCorrectionCategory("ATTRIBUTE");
    setCorrectionError(null);
    setCorrectionDialogOpen(true);
  };

  const submitCorrection = async () => {
    if (!currentBank || !observerId || !targetId || !correctionText.trim()) {
      setCorrectionError(t("correctionRequired"));
      return;
    }
    setSavingCorrection(true);
    setCorrectionError(null);
    try {
      await client.createPeerCorrection(currentBank, observerId, targetId, {
        claim: {
          claim_type: correctionCategory,
          text: correctionText.trim(),
          source_kind: "manual",
        },
      });
      toast.success(t("correctionSubmitted"));
      setCorrectionDialogOpen(false);
      await loadDirectionalContext();
    } catch (error) {
      setCorrectionError(getErrorMessage(error));
    } finally {
      setSavingCorrection(false);
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
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        <span>{t("sourceCount", { count: sourceCount })}</span>
        {origin && <span className="rounded border border-border px-1.5 py-0.5">{origin}</span>}
        {sourceIds.slice(0, 5).map((sourceId) => (
          <span key={sourceId} className="rounded border border-border/70 px-1.5 py-0.5 font-mono">
            {sourceId}
          </span>
        ))}
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
          {category && CARD_CATEGORIES.includes(category) && (
            <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-xs text-muted-foreground">
              {categoryLabels[category]}
            </span>
          )}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {renderEvidence(entry)}
          {claim.confidence != null && <span className="text-xs text-muted-foreground">{t("confidence", { value: Math.round(claim.confidence * 100) })}</span>}
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
        <Button size="sm" onClick={openCreatePeer}>
          <Plus className="mr-2 h-4 w-4" />
          {t("addPeer")}
        </Button>
      </div>

      {peerError && (
        <Alert variant="destructive">
          <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{peerError}</span>
            <Button variant="outline" size="sm" onClick={() => void loadPeers()} disabled={loadingPeers}>
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
                      <p className="truncate font-medium text-foreground" title={peer.id}>{peer.id}</p>
                      <p className="mt-1 text-xs text-muted-foreground">{kindLabels[peer.kind] ?? peer.kind}</p>
                    </div>
                    <Button variant="ghost" size="sm" onClick={() => openEditPeer(peer)} aria-label={t("editPeerAria", { id: peer.id })}>
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
                  <SelectTrigger id="peer-observer"><SelectValue placeholder={t("selectPeer")} /></SelectTrigger>
                  <SelectContent>
                    {peers.map((peer) => <SelectItem key={peer.id} value={peer.id}>{peer.id}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <ArrowRight className="hidden h-5 w-5 text-muted-foreground md:block" aria-hidden="true" />
              <div className="space-y-2">
                <Label htmlFor="peer-target">{t("targetLabel")}</Label>
                <Select value={targetId} onValueChange={setTargetId}>
                  <SelectTrigger id="peer-target"><SelectValue placeholder={t("selectPeer")} /></SelectTrigger>
                  <SelectContent>
                    {peers.map((peer) => <SelectItem key={peer.id} value={peer.id}>{peer.id}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border/70 bg-muted/20 px-3 py-2 text-sm">
              <span className="font-medium text-foreground">{observer?.id}</span>
              <ArrowRight className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              <span className="font-medium text-foreground">{target?.id}</span>
              {observerId === targetId && <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-xs text-primary">{t("selfPair")}</span>}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => void runModelAction("model")} disabled={loadingContext || operation !== null}>
                {operation === "model" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
                {operation === "model" ? t("modeling") : t("model")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => void runModelAction("rebuild")} disabled={loadingContext || operation !== null}>
                {operation === "rebuild" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RotateCcw className="mr-2 h-4 w-4" />}
                {operation === "rebuild" ? t("rebuilding") : t("rebuild")}
              </Button>
              <Button size="sm" variant="outline" onClick={openCorrection} disabled={loadingContext || operation !== null}>
                <Pencil className="mr-2 h-4 w-4" />
                {t("addCorrection")}
              </Button>
            </div>
            {operationFeedback && (
              <Alert>
                <Check className="h-4 w-4" />
                <AlertTitle>{t("operationFeedbackTitle")}</AlertTitle>
                <AlertDescription>
                  {operationFeedback.kind === "model" ? t("modelOperationFeedback") : t("rebuildOperationFeedback")}
                  {operationFeedback.operationId && <span className="ml-1 font-mono text-xs">{operationFeedback.operationId}</span>}
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
                <Button variant="outline" size="sm" onClick={() => void loadDirectionalContext()} disabled={loadingContext}>{t("retry")}</Button>
              </AlertDescription>
            </Alert>
          )}
          {loadingContext ? (
            <Card><CardContent className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />{t("loadingContext")}</CardContent></Card>
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
                      <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">{categoryLabels[category]}</h3>
                      {card[category].length === 0 ? (
                        <p className="mt-3 text-sm text-muted-foreground">{t("noCategoryClaims")}</p>
                      ) : (
                        <div className="mt-3 space-y-3">
                          {card[category].map((entry, index) => {
                            const record = entry as Record<string, unknown>;
                            return <div key={String(entry.id ?? index)} className="border-l-2 border-primary/40 pl-3"><p className="text-sm text-foreground whitespace-pre-wrap">{getEntryText(record)}</p><div className="mt-2">{renderEvidence(record)}</div></div>;
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
                    if (representation == null) return <p className="text-sm text-muted-foreground">{t("notAvailable")}</p>;
                    return <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-border/70 bg-muted/20 p-4 font-mono text-sm text-foreground">{typeof representation === "string" ? representation : JSON.stringify(representation, null, 2)}</pre>;
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
                    [...CLAIM_STATUSES, ...Object.keys(claimGroups).filter((status) => !CLAIM_STATUSES.includes(status as ClaimStatusGroup))].map((status) => {
                      const statusClaims = claimGroups[status] ?? [];
                      return <section key={status}><h3 className="mb-2 text-sm font-semibold text-foreground">{statusLabels[status as ClaimStatusGroup] ?? status}</h3>{statusClaims.length === 0 ? <p className="text-sm text-muted-foreground">{t("noClaimsInStatus")}</p> : <div className="space-y-2">{statusClaims.map(renderClaim)}</div>}</section>;
                    })
                  )}
                </CardContent>
              </Card>

              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                {context?.version != null && <span>{t("version", { version: context.version })}</span>}
                {context?.updated_at && <span>{t("updatedAt", { date: new Date(context.updated_at).toLocaleString() })}</span>}
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
            <DialogDescription>{editingPeer ? t("editPeerDescription") : t("createPeerDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="peer-id">{t("peerIdLabel")}</Label>
              <Input id="peer-id" value={peerForm.id} disabled={editingPeer !== null} onChange={(event) => setPeerForm((current) => ({ ...current, id: event.target.value }))} placeholder={t("peerIdPlaceholder")} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="peer-kind">{t("kindLabel")}</Label>
              <Select value={peerForm.kind} onValueChange={(kind) => setPeerForm((current) => ({ ...current, kind }))}>
                <SelectTrigger id="peer-kind"><SelectValue /></SelectTrigger>
                <SelectContent>{PEER_KINDS.map((kind) => <SelectItem key={kind} value={kind}>{kindLabels[kind]}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="peer-metadata">{t("metadataLabel")}</Label>
              <Textarea id="peer-metadata" value={peerForm.metadata} onChange={(event) => setPeerForm((current) => ({ ...current, metadata: event.target.value }))} placeholder={t("metadataPlaceholder")} rows={6} />
              <p className="text-xs text-muted-foreground">{t("metadataHelp")}</p>
            </div>
            {peerFormError && <Alert variant="destructive"><AlertDescription>{peerFormError}</AlertDescription></Alert>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPeerDialogOpen(false)} disabled={savingPeer}>{t("cancel")}</Button>
            <Button onClick={() => void savePeer()} disabled={savingPeer || !peerForm.id.trim()}>{savingPeer && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{editingPeer ? t("savePeer") : t("createPeer")}</Button>
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
              <Label htmlFor="correction-category">{t("categoryLabel")}</Label>
              <Select value={correctionCategory} onValueChange={(category) => setCorrectionCategory(category as CardCategory)}>
                <SelectTrigger id="correction-category"><SelectValue /></SelectTrigger>
                <SelectContent>{CARD_CATEGORIES.map((category) => <SelectItem key={category} value={category}>{categoryLabels[category]}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="correction-text">{t("correctionTextLabel")}</Label>
              <Textarea id="correction-text" value={correctionText} onChange={(event) => setCorrectionText(event.target.value)} placeholder={t("correctionTextPlaceholder")} rows={6} />
            </div>
            <Alert><AlertDescription>{t("correctionLockedNotice")}</AlertDescription></Alert>
            {correctionError && <Alert variant="destructive"><AlertDescription>{correctionError}</AlertDescription></Alert>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCorrectionDialogOpen(false)} disabled={savingCorrection}>{t("cancel")}</Button>
            <Button onClick={() => void submitCorrection()} disabled={savingCorrection || !correctionText.trim()}>{savingCorrection && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("submitCorrection")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
