import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { ApiError, getMe, setUnauthorizedHandler, type Me } from "./api";
import { Navbar } from "./components/Navbar";
import { HistoryPage } from "./pages/HistoryPage";
import { MetricsPage } from "./pages/MetricsPage";
import { WorkspacePage } from "./pages/WorkspacePage";

async function fetchMe(): Promise<Me | null> {
  try {
    return await getMe();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export default function App() {
  const queryClient = useQueryClient();
  const [authOpen, setAuthOpen] = useState(false);
  const meQuery = useQuery({ queryKey: ["me"], queryFn: fetchMe, retry: false });
  const me = meQuery.data ?? null;

  useEffect(() => {
    // Any 401 from a protected call: clear the session and open the login modal.
    setUnauthorizedHandler(() => {
      queryClient.setQueryData(["me"], null);
      setAuthOpen(true);
    });
  }, [queryClient]);

  const open = () => setAuthOpen(true);
  return (
    <>
      <Navbar me={me} authOpen={authOpen} onAuthOpen={open} onAuthClose={() => setAuthOpen(false)} />
      <main>
        <Routes>
          <Route path="/" element={<WorkspacePage me={me} onAuthOpen={open} />} />
          <Route path="/history" element={<HistoryPage me={me} onAuthOpen={open} />} />
          <Route path="/metrics" element={<MetricsPage me={me} />} />
        </Routes>
      </main>
    </>
  );
}
