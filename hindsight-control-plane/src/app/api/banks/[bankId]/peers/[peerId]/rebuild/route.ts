import { missingPeerTargetResponse, proxyPeerRequest } from "@/lib/peer-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  const target = new URL(request.url).searchParams.get("target");
  if (!target) return missingPeerTargetResponse(request);
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/rebuild/${encodeURIComponent(target)}`,
    {
      method: "POST",
      errorKey: "api.errors.peers.rebuild",
      fallbackMessage: "Failed to rebuild peer model",
    }
  );
}
