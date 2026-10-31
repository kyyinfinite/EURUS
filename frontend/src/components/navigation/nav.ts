/** Navigation model — single source of truth for sidebar, drawer and palette. */

import {
  Activity,
  Bot,
  Boxes,
  Brain,
  CircleCheck,
  Container,
  FileStack,
  KeyRound,
  LayoutDashboard,
  MessagesSquare,
  ScrollText,
  Settings as Config,
  ShieldCheck,
  Sparkles,
  Workflow,
  BarChart3,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  description: string;
  badge?: "approvals";
}

export interface NavGroup {
  name: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    name: "Workspace",
    items: [
      { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true, description: "Gateway overview, activity and health" },
      { to: "/home", label: "New Task", icon: Sparkles, description: "Prompt-first welcome surface" },
      { to: "/chat", label: "Chat", icon: MessagesSquare, description: "Full conversation workspace" },
      { to: "/sessions", label: "Conversations", icon: Container, description: "Browse and inspect sessions" },
      { to: "/files", label: "Files", icon: FileStack, description: "Uploaded media and attachments" },
    ],
  },
  {
    name: "Intelligence",
    items: [
      { to: "/agents", label: "Agents", icon: Bot, description: "Agent profile and model chain" },
      { to: "/workflows", label: "Workflows", icon: Workflow, description: "Repeatable task pipelines" },
      { to: "/tools", label: "Tools & MCP", icon: Boxes, description: "Tool registry and MCP servers" },
      { to: "/memory", label: "Memory", icon: Brain, description: "Long-lived memory store" },
      { to: "/providers", label: "Providers", icon: KeyRound, description: "Model providers and capabilities" },
    ],
  },
  {
    name: "Operations",
    items: [
      { to: "/runtime", label: "Runtime", icon: Activity, description: "Runs, events and diagnostics" },
      { to: "/approvals", label: "Approvals", icon: CircleCheck, description: "Pending tool approvals", badge: "approvals" },
      { to: "/analytics", label: "Usage", icon: BarChart3, description: "Run volume and outcomes" },
    ],
  },
  {
    name: "System",
    items: [
      { to: "/security", label: "Access & Security", icon: ShieldCheck, description: "Gateway posture and access" },
      { to: "/config", label: "Settings", icon: Config, description: "Configuration editor" },
      { to: "/logs", label: "System Logs", icon: ScrollText, description: "Redacted gateway log stream" },
    ],
  },
];

export const NAV_FLAT: NavItem[] = NAV_GROUPS.flatMap((group) => group.items);

export const SHELL_METRICS = { sidebarWidth: 260, sidebarCollapsedWidth: 72, topbarHeight: 68 };
