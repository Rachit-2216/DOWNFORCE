import Link from "next/link";

export function AppHeader({ context }: { context: string }) {
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="app-header">
        <Link className="wordmark" href="/">
          DOWNFORCE
        </Link>
        <nav aria-label="Product navigation">
          <Link href="/app">Explore</Link>
          <Link href="/app/analytics">Analytics</Link>
          <Link href="/app/replays">Detailed replays</Link>
        </nav>
        <span>{context}</span>
      </header>
    </>
  );
}
