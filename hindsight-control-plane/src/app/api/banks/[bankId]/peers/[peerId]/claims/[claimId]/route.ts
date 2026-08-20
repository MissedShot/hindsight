import {
  invalidPeerJsonResponse,
  missingPeerTargetResponse,
  proxyPeerRequest,
} from "@/lib/peer-proxy";

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string; claimId: string }> }
) {
  const { bankId, peerId, claimId } = await params;
  const target = new URL(request.url).searchParams.get("target");
  if (!target) return missingPeerTargetResponse(request);

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return invalidPeerJsonResponse(request);
  }
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/claims/${encodeURIComponent(target)}/${encodeURIComponent(claimId)}`,
    {
      method: "PATCH",
      body,
      errorKey: "api.errors.peers.model",
      fallbackMessage: "Failed to update peer claim",
    }
  );
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string; claimId: string }> }
) {
  const { bankId, peerId, claimId } = await params;
  const target = new URL(request.url).searchParams.get("target");
  if (!target) return missingPeerTargetResponse(request);
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/claims/${encodeURIComponent(target)}/${encodeURIComponent(claimId)}`,
    {
      method: "DELETE",
      errorKey: "api.errors.peers.model",
      fallbackMessage: "Failed to delete peer claim",
    }
  );
}
