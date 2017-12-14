import bpy
import tempfile
import os


class ImageExporter(object):
    """
    This is a singleton
    """
    temp_images = []

    @classmethod
    def save_to_temp_file(cls, image, scene):
        temp_image = tempfile.NamedTemporaryFile(delete=False)
        cls.temp_images.append(temp_image)
        image.save_render(temp_image.name, scene)
        return temp_image.name

    @classmethod
    def export(cls, image, scene):
        if image.source == "GENERATED":
            return cls.save_to_temp_file(image, scene)
        elif image.source == "FILE":
            if image.packed_file:
                return cls.save_to_temp_file(image, scene)
            else:
                return bpy.path.abspath(image.filepath, library=image.library)
        elif image.source == "SEQUENCE":
            # TODO
            raise NotImplementedError("Sequence not supported yet")
        else:
            raise Exception('Unsupported image source "%s" in image "%s"' % (image.source, image.name))

    @classmethod
    def cleanup(cls):
        for temp_image in cls.temp_images:
            print("Deleting temporary image:", temp_image.name)
            os.remove(temp_image.name)

