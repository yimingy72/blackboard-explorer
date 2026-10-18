import { memo } from 'react';
import { Bubble, type BubbleListProps } from '@ant-design/x';

const roles: BubbleListProps['role'] = {
  assistant: { variant: 'borderless', styles: {
    root: { paddingBlock: 6, paddingInlineEnd: 0, width: '100%', minWidth: 0 },
    content: { padding: 0, minHeight: 0, width: '100%' },
    body: { gap: 4, width: '100%', minWidth: 0 },
    header: { marginBottom: 2 },
  } },
  user: { placement: 'end', styles: {
    root: { paddingBlock: 6, paddingInlineStart: '12%' },
    content: { padding: '7px 10px', minHeight: 0 },
    header: { marginBottom: 2 },
  } },
};

export default memo(function ConversationBubbles(props: Omit<BubbleListProps, 'role'>) {
  return <Bubble.List {...props} role={roles} />;
});
