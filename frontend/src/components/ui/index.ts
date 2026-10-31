/** UI kit barrel — the design system entrypoint.
 *
 *  Every workspace module imports primitives from "components/ui" only;
 *  nothing is duplicated across pages. Layout tokens: sidebar 260px,
 *  topbar 68px, cards rounded-xl, primary copper #995329.
 */

export { Button, IconButton, type ButtonProps, type ButtonVariant } from "./Button";
export { Badge, StatusDot, StatusIndicator, type Tone } from "./Badge";
export { Card, MiniBars } from "./Card";
export {
  Empty,
  EmptyState,
  ErrorNote,
  ErrorState,
  KeyValue,
  LoadingState,
  Skeleton,
  Spinner,
} from "./States";
export { Modal } from "./Modal";
export { Drawer } from "./Drawer";
export {
  Dropdown,
  DropdownContent,
  DropdownItem,
  DropdownLabel,
  DropdownSeparator,
  DropdownTrigger,
  Popover,
  PopoverControl,
  Tooltip,
  TooltipProvider,
} from "./Overlays";
export { Tabs, TabsList, TabsTrigger, TabsContent } from "./Tabs";
export { Input, Textarea, Select, SearchInput, FormField } from "./Input";
export { PageHeader } from "./PageHeader";
export { MetricCard } from "./MetricCard";
export { DataTable, type DataTableColumn } from "./DataTable";
export { ToastViewport } from "./Toasts";
export { useCountdown, useDismiss, useHotkey, useMediaQuery, useAutoScroll } from "../../hooks/useInteractions";
