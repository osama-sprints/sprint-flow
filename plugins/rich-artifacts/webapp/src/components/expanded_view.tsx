import React from 'react';
import Modal from 'react-modal';

type Props = {
    title: string;
    onClose: () => void;
    children: React.ReactNode;
};

/**
 * The expanded view of one artifact.
 *
 * react-modal rather than a hand-rolled overlay: it brings the parts that are
 * easy to get wrong and tedious to re-test — focus trapping, focus restoration
 * on close, Escape handling, `aria-modal` and the inert background — which the
 * previous custom overlay did not have. Mattermost's own modal components are
 * not part of the plugin API, so they are not an option here.
 *
 * Styling stays ours through class names, so the dialog follows the viewer's
 * Mattermost theme.
 */

/**
 * Point react-modal at the application root so it can hide the background from
 * assistive technology. Mattermost renders into #root; when that is missing
 * (tests, an unexpected shell) the warning is suppressed rather than crashing
 * the post.
 */
const setAppElement = (): void => {
    const root = document.getElementById('root') || document.body;
    try {
        Modal.setAppElement(root);
    } catch (e) {
        // Nothing to attach to; the dialog still renders and still traps focus.
    }
};

const ExpandedView = ({title, onClose, children}: Props) => {
    React.useEffect(setAppElement, []);

    return (
        <Modal
            isOpen={true}
            onRequestClose={onClose}
            contentLabel={title}
            className='sf-artifact__dialog'
            overlayClassName='sf-artifact__overlay'
            shouldCloseOnEsc={true}
            shouldCloseOnOverlayClick={true}
            shouldReturnFocusAfterClose={true}
        >
            <div className='sf-artifact__dialog-header'>
                <span className='sf-artifact__title'>{title}</span>
                <button
                    className='sf-artifact__button'
                    onClick={onClose}
                >{'Close'}</button>
            </div>
            <div className='sf-artifact__dialog-body'>{children}</div>
        </Modal>
    );
};

export default ExpandedView;
