import {
  proxyPeerRequest,
  invalidPeerJsonResponse,
  missingPeerTargetResponse,
} from "@/lib/peer-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return invalidPeerJsonResponse(request);
  }

  const target = new URL(request.url).searchParams.get("target");
  if (!target) return missingPeerTargetResponse(request);
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/corrections/${encodeURIComponent(target)}/plan`,
    {
      method: "POST",
      body,
      errorKey: "api.errors.peers.correction",
      fallbackMessage: "Failed to plan peer correction",
    }
  );
}
