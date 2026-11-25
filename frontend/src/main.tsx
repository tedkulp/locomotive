import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

import { Layout } from "@/components/Layout";
import { HistoryPage } from "@/pages/History";
import { LaunchPage } from "@/pages/Launch";
import { PlaceholderPage } from "@/pages/Placeholder";
import { CurrentRunPage, RunPage } from "@/pages/RunPage";
import "./index.css";

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <LaunchPage /> },
      { path: "/runs/current", element: <CurrentRunPage /> },
      { path: "/runs/:id", element: <RunPage /> },
      { path: "/history", element: <HistoryPage /> },
      { path: "/schedules", element: <PlaceholderPage title="Schedules" /> },
      { path: "/status", element: <PlaceholderPage title="Status" /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
