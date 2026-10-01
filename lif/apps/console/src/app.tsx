// Router + auth gate. Shell, Home and Ask load eagerly (first paint and the ≤2-interaction mobile Ask
// path, §13); every other screen is a lazy chunk (§92, §93).
import type { ComponentChildren } from 'preact';
import { useEffect } from 'preact/hooks';
import { ErrorBoundary, default as lazy } from 'preact-iso/lazy';
import { LocationProvider, Route, Router, useLocation } from 'preact-iso/router';
import { get } from '@/api/client';
import type { SetupState } from '@/api/contracts.gen';
import { useMe } from '@/api/session';
import * as sse from '@/api/sse';
import { Layout } from '@/shell/Layout';
import { useBreakpoint } from '@/shell/useBreakpoint';
import { HumanErrorCard } from '@/ui/HumanErrorCard';
import { Skeleton } from '@/ui/Skeleton';
import { Toaster } from '@/ui/Toast';
import Home from '@/pages/home/Home';
import Ask from '@/pages/ask/Ask';

const Agents = lazy(() => import('@/pages/agents/Agents'));
const AgentDetail = lazy(() => import('@/pages/agents/AgentDetail'));
const Models = lazy(() => import('@/pages/models/Models'));
const RoleDetail = lazy(() => import('@/pages/models/RoleDetail'));
const DeploymentDetail = lazy(() => import('@/pages/models/DeploymentDetail'));
const Discovery = lazy(() => import('@/pages/models/Discovery'));
const Candidate = lazy(() => import('@/pages/models/Candidate'));
const Jobs = lazy(() => import('@/pages/jobs/Jobs'));
const JobDetail = lazy(() => import('@/pages/jobs/JobDetail'));
const Knowledge = lazy(() => import('@/pages/knowledge/Knowledge'));
const KnowledgeObject = lazy(() => import('@/pages/knowledge/KnowledgeObject'));
const System = lazy(() => import('@/pages/system/System'));
const Connect = lazy(() => import('@/pages/connect/Connect'));
const Trust = lazy(() => import('@/pages/connect/Trust'));
const Login = lazy(() => import('@/pages/auth/Login'));
const Setup = lazy(() => import('@/pages/auth/Setup'));
const Pair = lazy(() => import('@/pages/auth/Pair'));
const NotFound = lazy(() => import('@/pages/NotFound'));

/** Pages that work without a session (no shell, no SSE). */
const PUBLIC_PATHS = /^\/(login|setup|pair)(\/|$)/;

function Splash() {
  return (
    <div class="page" aria-busy="true" aria-label="Loading Labzilla">
      <Skeleton height="28px" width="40%" />
      <Skeleton lines={3} />
    </div>
  );
}

/** Where a signed-out visitor goes: first-run setup while no admin exists (§83), otherwise sign in. */
async function signedOutTarget(next: string): Promise<string> {
  try {
    const setup = await get<SetupState>('/api/setup', { allow401: true });
    if (setup.needs_setup) return '/setup';
  } catch {
    /* can't tell: the login page re-checks and explains */
  }
  return next === '/' ? '/login' : `/login?next=${encodeURIComponent(next)}`;
}

/** Confirms the session before rendering the app shell; starts the live event stream once signed in. */
function AuthGate({ children }: { children: ComponentChildren }) {
  const me = useMe();
  const { url, route } = useLocation();
  // Only trust "signed out" once no refetch is in flight (a fresh login primes the cache via signedIn()).
  const signedOut = me.data === null && !me.refreshing;
  useEffect(() => {
    if (me.data) sse.start();
    if (!signedOut) return;
    sse.stop();
    let live = true;
    void signedOutTarget(url).then((to) => live && route(to, true));
    return () => {
      live = false;
    };
  }, [me.data, signedOut]);
  if (me.error) return <div class="page"><HumanErrorCard error={me.error} onRetry={() => void me.refresh()} /></div>;
  if (!me.data) return <Splash />;
  return <>{children}</>;
}

function Shell() {
  const { path } = useLocation();
  const bp = useBreakpoint();
  if (PUBLIC_PATHS.test(path)) {
    return (
      <main id="main" class="lz-public">
        <Router>
          <Route path="/login" component={Login} />
          <Route path="/setup" component={Setup} />
          <Route path="/pair" component={Pair} />
          <Route default component={NotFound} />
        </Router>
        <Toaster />
      </main>
    );
  }
  // Ask has its own composer; compact Home is the Mobile Gateway with the prompt already on screen (§12).
  const ownPrompt = path.startsWith('/ask') || (path === '/' && bp === 'compact');
  return (
    <AuthGate>
      <Layout hideCommandBar={ownPrompt}>
        <Router>
          <Route path="/" component={Home} />
          <Route path="/ask" component={Ask} />
          <Route path="/ask/:id" component={Ask} />
          <Route path="/agents" component={Agents} />
          <Route path="/agents/:id" component={AgentDetail} />
          <Route path="/models" component={Models} />
          <Route path="/models/roles/:role" component={RoleDetail} />
          <Route path="/models/deployments/:id" component={DeploymentDetail} />
          <Route path="/models/discovery" component={Discovery} />
          <Route path="/models/candidates/:id" component={Candidate} />
          <Route path="/jobs" component={Jobs} />
          <Route path="/jobs/:id" component={JobDetail} />
          <Route path="/knowledge" component={Knowledge} />
          <Route path="/knowledge/o/:objectKey+" component={KnowledgeObject} />
          <Route path="/system" component={System} />
          <Route path="/system/:tab" component={System} />
          <Route path="/connect" component={Connect} />
          <Route path="/trust" component={Trust} />
          <Route default component={NotFound} />
        </Router>
      </Layout>
    </AuthGate>
  );
}

export function App() {
  return (
    <LocationProvider>
      <ErrorBoundary>
        <Shell />
      </ErrorBoundary>
    </LocationProvider>
  );
}
