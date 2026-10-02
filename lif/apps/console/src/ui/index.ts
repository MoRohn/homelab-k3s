// Labzilla UI kit: a small library (spec §58, §85). Pages import from '@/ui'.
// Styles: src/styles/components.css, loaded once globally via ui.css from main.tsx (the shell renders kit
// parts before any page loads). Inventory and conventions: README.md next to this file.

// primitives
export { Button, type ButtonProps, type ButtonVariant, type ButtonSize } from './Button';
export { IconButton, type IconButtonProps } from './IconButton';
export { Input, type InputProps } from './Input';
export { Textarea, type TextareaProps } from './Textarea';
export { Select, type SelectProps, type SelectOption } from './Select';
export { Switch, type SwitchProps } from './Switch';
export { Card, type CardProps } from './Card';
export { List, ListItem, type ListProps, type ListItemProps } from './List';
export { safeHref } from './href';
export { Table, type TableProps, type Column } from './Table';
export { Modal, type ModalProps } from './Modal';
export { Drawer, type DrawerProps } from './Drawer';
export { Dialog, type DialogProps } from './Dialog';
export { Sheet, type SheetProps } from './Sheet';
export { Tabs, TabPanel, type TabsProps, type TabItem, type TabPanelProps } from './Tabs';
export { Badge, type BadgeProps } from './Badge';
export { Tooltip, type TooltipProps } from './Tooltip';
export { Progress, type ProgressProps } from './Progress';
export { CodeBlock, type CodeBlockProps } from './CodeBlock';
export { Toaster, toast, dismissToast, type ToastOptions, type ToasterProps } from './Toast';
export { Skeleton, type SkeletonProps } from './Skeleton';
export { EmptyState, type EmptyStateProps, type EmptyAction } from './EmptyState';
export { Icon, ICON_NAMES, type IconName, type IconProps } from './Icon';
export { CommandInput, type CommandInputProps } from './CommandInput';
export { FactList, type FactListProps, type FactItem } from './FactList';
export { Markdown, type MarkdownProps } from './Markdown';

// domain
export { StatusDot, type StatusDotProps } from './StatusDot';
export { StatusBadge, type StatusBadgeProps } from './StatusBadge';
export { ResourceBar, type ResourceBarProps, type ResourceSegment, type SegmentTone } from './ResourceBar';
export { ActivityRow, type ActivityRowProps } from './ActivityRow';
export { ApprovalCard, type ApprovalCardProps } from './ApprovalCard';
export { ModelCard, type ModelCardProps } from './ModelCard';
export { AgentCard, type AgentCardProps } from './AgentCard';
export { JobRow, type JobRowProps } from './JobRow';
export { DecisionBadge, type DecisionBadgeProps } from './DecisionBadge';
export { RouteTrail, type RouteTrailProps } from './RouteTrail';
export { PrivacyBadge, type PrivacyBadgeProps } from './PrivacyBadge';
export { TechDetails, type TechDetailsProps } from './TechDetails';
export { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog';
export { HumanErrorCard, type HumanErrorCardProps } from './HumanErrorCard';
export { Timeline, type TimelineProps } from './Timeline';

// shared vocabulary
export { HEALTH, SEVERITY, cx, type Tone, type HealthMeta } from './tone';
export * as fmt from './format';
export { copyText, useCopy } from './clipboard';
