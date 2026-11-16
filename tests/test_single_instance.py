"""Only one gateway at a time on one data folder."""
import pytest

from printtoken.app import AlreadyRunning, single_instance


def test_a_second_gateway_on_the_same_data_folder_is_refused(tmp_path):
    first = single_instance(tmp_path)
    with pytest.raises(AlreadyRunning):
        single_instance(tmp_path)
    first.close()                               # the first one stops: the next start works
    single_instance(tmp_path).close()
