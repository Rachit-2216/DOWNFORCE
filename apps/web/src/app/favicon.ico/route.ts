export function GET() {
  const icon = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="3" fill="#030914"/><path d="M7 8h10l4 4v12H7zm4 5v6h5v-6z" fill="#f5f7fb"/><path d="M21 8h4v16h-4z" fill="#1e41ff"/><path d="M21 8h4v4h-4z" fill="#e10600"/></svg>`;
  return new Response(icon, {
    headers: {
      "Cache-Control": "public, max-age=86400",
      "Content-Type": "image/svg+xml",
    },
  });
}
