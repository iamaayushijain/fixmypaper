import { NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendUrl() {
  return "http://localhost:7860".replace(/\/$/, "");
}

function getUploadTimeoutMs() {
  const raw = "900000";
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return 900000;
  }
  return parsed;
}

/**
 * Proxies the multipart upload to Flask.
 * This avoids Next's rewrite proxy instability/timeouts during long PDF processing.
 */
export async function POST(request) {
  const API = getBackendUrl();
  const timeoutMs = getUploadTimeoutMs();

  // Parse multipart from the client.
  const incoming = await request.formData();

  const fd = new FormData();
  const file = incoming.get("file");
  if (!file || typeof file.arrayBuffer !== "function") {
    return NextResponse.json({ error: "No PDF file provided" }, { status: 400 });
  }

  // Ensure we proxy as a Blob/File to keep the multipart structure intact.
  const buf = await file.arrayBuffer();
  const blob = new Blob([buf], { type: file.type || "application/pdf" });
  fd.append("file", blob, file.name || "upload.pdf");

  const formatId = incoming.get("format_id");
  if (formatId) fd.append("format_id", formatId);

  const startPage = incoming.get("start_page");
  if (startPage) fd.append("start_page", startPage);

  const controller = new AbortController();
  const timeout = timeoutMs > 0
    ? setTimeout(() => controller.abort(), timeoutMs)
    : null;

  try {
    const backendRes = await fetch(`${API}/upload`, {
      method: "POST",
      body: fd,
      signal: controller.signal,
    });

    // Return backend response as-is.
    const contentType = backendRes.headers.get("content-type");
    const body = await backendRes.arrayBuffer();
    return new NextResponse(body, {
      status: backendRes.status,
      headers: contentType ? { "content-type": contentType } : undefined,
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      const secs = Math.round(timeoutMs / 1000);
      return NextResponse.json(
        {
          error: `Upload timed out after ${secs} seconds. Set UPLOAD_PROXY_TIMEOUT_MS to a higher value for large PDFs.`,
        },
        { status: 504 },
      );
    }

    console.error("[UPLOAD PROXY] Failed to reach backend:", error);
    return NextResponse.json(
      { error: "Upload proxy could not reach backend service" },
      { status: 502 },
    );
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

