import { missingPeerTargetResponse, proxyPeerRequest } from "@/lib/peer-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  const target = new URL(request.url).searchParams.get("target");
  if (!target) return missingPeerTargetResponse(request);
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/claims/${encodeURIComponent(target)}`,
    {
      errorKey: "api.errors.peers.claims",
      fallbackMessage: "Failed to fetch peer claims",
    }
  );
}
