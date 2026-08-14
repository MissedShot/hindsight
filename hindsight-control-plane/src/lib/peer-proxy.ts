import { NextResponse } from "next/server";
import { dataplaneBankUrl, getDataplaneHeaders } from "@/lib/hindsight-client";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";

interface ProxyOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  errorKey: string;
  fallbackMessage: string;
}

export function invalidPeerJsonResponse(request: Request) {
  return NextResponse.json(
    localizeApiErrorPayload(request, {
      error: "Invalid request body",
      errorKey: "api.errors.auth.invalidRequestBody",
    }),
    { status: 400 }
  );
}

export function missingPeerTargetResponse(request: Request) {
  return NextResponse.json(
    localizeApiErrorPayload(request, {
      error: "Missing target peer",
      errorKey: "api.errors.auth.invalidRequestBody",
    }),
    { status: 400 }
  );
}

/**
 * Proxy the peer surface without depending on the generated dataplane SDK.
 * Credentials stay in the server-side dataplane helper, while upstream status
 * and safe, localized error payloads are preserved for the browser client.
 */
export async function proxyPeerRequest(
  request: Request,
  bankId: string,
  suffix: string,
  options: ProxyOptions
) {
  let response: Response;
  try {
    response = await fetch(dataplaneBankUrl(bankId, suffix), {
      method: options.method ?? "GET",
      headers: getDataplaneHeaders({ "Content-Type": "application/json" }),
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
    });
  } catch {
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: options.fallbackMessage,
        errorKey: options.errorKey,
      }),
      { status: 502 }
    );
  }

  const responseText = await response.text();
  let data: unknown;
  try {
    data = responseText ? JSON.parse(responseText) : null;
  } catch {
    data = responseText ? { detail: responseText } : null;
  }

  if (!response.ok) {
    // Keep useful validation/auth details from 4xx dataplane responses while
    // using the localized control-plane fallback as the primary error. Never
    // expose opaque upstream 5xx bodies through the browser boundary.
    const payload = {
      error: options.fallbackMessage,
      errorKey: options.errorKey,
      ...(response.status < 500 && data !== null ? { details: data } : {}),
    };
    return NextResponse.json(localizeApiErrorPayload(request, payload), {
      status: response.status,
    });
  }

  return NextResponse.json(data, { status: response.status });
}
