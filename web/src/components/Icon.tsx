import { ArrowLeftOutlined, ArrowRightOutlined, ArrowUpOutlined, CheckOutlined, CloseOutlined, CodeOutlined, CopyOutlined, DownloadOutlined, FileTextOutlined, MoreOutlined, PlusOutlined, ReloadOutlined, RightOutlined, SearchOutlined, ExportOutlined } from '@ant-design/icons';
const icons = { back: ArrowLeftOutlined, arrow: ArrowRightOutlined, send: ArrowUpOutlined, close: CloseOutlined, more: MoreOutlined, refresh: ReloadOutlined, file: FileTextOutlined, download: DownloadOutlined, external: ExportOutlined, chevron: RightOutlined, plus: PlusOutlined, check: CheckOutlined, terminal: CodeOutlined, search: SearchOutlined, copy: CopyOutlined };
export default function Icon({ name, className }: { name: keyof typeof icons; className?: string }) {
  const Component = icons[name];
  return <Component aria-hidden className={className} />;
}
