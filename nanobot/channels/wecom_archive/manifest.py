"""WeCom archive channel management contract."""

from nanobot.channels._manifest import field
from nanobot.channels.contracts import ChannelSetupSpec
from nanobot.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "allowFrom": field("list"),
        "injectHost": field(),
        "injectPort": field(),
        "injectPath": field(),
        "injectToken": field("secret"),
        "hubBaseUrl": field(),
        "deviceId": field(),
        "deviceSecret": field("secret"),
        "downloadMedia": field("bool"),
    },
    required=(),
    official_url="https://developer.work.weixin.qq.com/document/path/91774",
)

PLUGIN = ChannelPlugin(
    name="wecom_archive",
    display_name="WeCom Archive",
    runtime=f"{__package__}.runtime:WecomArchiveChannel",
    setup=SETUP_SPEC,
    dependencies=(),
)
